import numpy as np
from uvtools import dspec
import tensorflow as tf
from pyuvdata import UVData, UVCal
from . import utils
import copy
import tqdm
import argparse
import itertools

OPTIMIZERS = {'Adadelta': tf.optimizers.Adadelta, 'Adam': tf.optimizers.Adam, 'Adamax':tf.optimizers.Adamax,
              'Ftrl': tf.optimizers.Ftrl, 'Nadam':tf.optimizers.Nadam, 'SGD':tf.optimizers.SGD, 'RMSprop': tf.optimizers.RMSprop}


def calibrate_and_model_per_baseline(uvdata, foreground_basis_vectors, gains=None, freeze_model=False,
                                     optimizer='Adamax', tol=1e-14, maxsteps=10000,
                                     verbose=False, sky_model=None, dtype_opt=np.float32,
                                     record_var_history=False, **opt_kwargs):
    """Perform simultaneous calibration and fitting of foregrounds --per baseline--.

    This approach gives up on trying to invert the wedge but can be used on practically any array.

    Parameters
    ----------
    uvdata: UVData object
        uvdata objet of data to be calibrated.
    foreground_basis_vectors: dictionary
        dictionary containing Nfreq x Nbasis design matrices
        describing the basis vectors being used to model each baseline with keys corresponding
        antenna pairs.
    gains: UVCal object
        UVCal with initial gain estimates.
        There many smart ways to obtain initial gain estimates
        but this is beyond the scope of calamity (for example, firstcal, logcal, sky-based cal).
        Users can determine initial gains with their favorite established cal algorithm.
        default is None -> start with unity gains.
        WARNING: At the present, the flags in gains are not propagated/used! Make sure flags in uvdata object!
    optimizer: string
        Name of optimizer. See OPTIMIZERS dictionary which contains optimizers described in
        https://www.tensorflow.org/api_docs/python/tf/keras/optimizers
        default is 'Adamax'
    tol: float, optional
        halting condition for optimizer loop. Stop loop when the change in the cost function falls
        below tol.
        default is 1e-14
    maxsteps: int, optional
        maximum number of opt.minimize calls before halting.
        default is 10000
    verbose: bool, optional
        generate lots of text.
        default is False.
    sky_model: UVData object, optional
        a sky-model to use for initial estimates of foreground coefficients and
        to set overall flux scale and phases.
        Note that this model is not used to obtain initial gain estimates.
        These must be provided through the gains argument.
    dtype_opt: numpy dtype, optional
        the float precision to be used in tensorflow gradient descent.
        runtime scales roughly inversely linear with precision.
        default is np.float32
    record_var_history: bool, optional
        keep detailed record of optimization history of variables.
        default is False.

    Returns
    -------
    uvdata_model: UVData object
        uvdata object containing model of the foregrounds multiplied by the gains
        (uncalibrated data). This model is mean to be subtracted from the data
        before applying gain solutions.
    uvcal_model: UVCal object
        uvcal object containing estimates of the gain solutions. These solutions
        are not referenced to any sky model and are likely orders of
    fitting_info:
        dictionary containing fit history with fields:
        'loss_history': list of values of the loss function in each minimization iteration.
        if record_var_history is True:
            each of the following fields is included which points to an Nstep x Nvar array of values
            'fg_r': real part of foreground coefficients
            'fg_i': imag part of foreground coefficients
            'g_r': real part of gains.
            'g_i': imag part of gains
    """
    uvdata = uvdata.select(inplace=False, bls=[ap for ap in uvdata.get_antpairs() if ap[0] != ap[1]])
    resid = copy.deepcopy(uvdata)
    model = copy.deepcopy(uvdata)
    filtered = copy.deepcopy(uvdata)
    if gains is None:
        gains = utils.blank_uvcal_from_uvdata()
    # if sky-model is None, initialize it to be the
    # data divided by the initial gain estimates.
    if sky_model is None:
        sky_model = uvdata
        for ap in sky_model.get_antpairs():
            for pnum, pol in enumerate(sky_model.get_pols()):
                blinds = sky_model.antpair2ind(ap)
                sky_model.data_array[blinds, 0, :, pnum] = sky_model.data_array[blinds, 0, :, pnum] / (gains.get_gains(ap[0], 'J' + pol) * np.conj(gains.get_gains(ap[1], 'J' + pol))).T


    fitting_info = {}
    for polnum, pol in enumerate(uvdata.get_pols()):
        fitting_info_p = {}
        rmsdata = np.sqrt(np.mean(np.abs(uvdata.data_array[:, :, :, polnum][~uvdata.flag_array[:, :, :, polnum]]) ** 2.))
        # pull data out of uvdata object and into a dict (to avoid slicing operations)
        data_dict = {}
        model_dict = {}
        resid_dict = {}
        flag_dict = {}
        nsample_dict = {}
        gain_dict = {}
        for ant in gains.antenna_numbers:
            gain_dict[ant] = gains.get_gains(ant, 'J' + pol)
        # extract raveled data into dictionaries of numpy arrays for fast access.
        for ap in uvdata.get_antpairs():
            bl = ap + (pol,)
            data_dict[ap] = copy.copy(uvdata.get_data(bl))
            model_dict[ap] = copy.copy(sky_model.get_data(bl))
            resid_dict[ap] = copy.copy(uvdata.get_data(bl))
            flag_dict[ap] = copy.copy(uvdata.get_flags(bl))
            nsample_dict[ap] = copy.copy(uvdata.get_nsamples(bl))

        # initialize foreground modeling coefficients.
        foreground_coeffs = {ap: None for ap in foreground_basis_vectors}
        for ap in foreground_basis_vectors:
            # gives Ntimes x Nbasis coefficients for each fg coeff.
            foreground_coeffs[ap] = model_dict[ap] @ foreground_basis_vectors[ap]

        # generate maps between antenna numbers and correlation indices that are
        # natural numbers (starting at zero).
        ant_map = {i: ant for i, ant in enumerate(gains.ant_array)}
        ant_mapi = {ant: i for ant, i in zip(ant_map.values(), ant_map.keys())}
        evec_map = {}
        for i, j in itertools.combinations(ant_map.keys(), 2):
            ap = (ant_map[i], ant_map[j])
            # conjugation does not apply to purely real fg basis vectors.
            if ap not in foreground_basis_vectors:
                ap = ap[::-1]
            evec_map[(i, j)] = tf.convert_to_tensor(foreground_basis_vectors[ap], dtype=dtype_opt)
        # map i, j to start-end indices of foreground vector.
        fgrange_map = {}
        startind = 0
        for i, j in evec_map.keys():
            fgrange_map[(i, j)] = (startind, startind + evec_map[(i, j)].shape[1])
            startind += evec_map[(i, j)].shape[1]

        # perform solutions on each time separately.
        for tnum in range(uvdata.Ntimes):
            data_map_r = {}
            data_map_i = {}
            weights_map = {}
            fg_r = []
            fg_i = []
            # map data and gains from particular time into correlation indexed
            # tensor arrays to be used in optimization.
            for i, j in itertools.combinations(ant_map.keys(), 2):
                ap = (ant_map[i], ant_map[j])
                isign = 1.
                # need to conjugate data.
                if ap not in data_dict:
                    ap = ap[::-1]
                    isign = -1.
                data_map_r[(i, j)] = tf.convert_to_tensor(data_dict[ap][tnum].real / rmsdata, dtype=dtype_opt)
                data_map_i[(i, j)] = tf.convert_to_tensor(isign * data_dict[ap][tnum].imag / rmsdata, dtype=dtype_opt)
                weights_map[(i, j)] = tf.convert_to_tensor((~flag_dict[ap]).astype(dtype_opt)[tnum] * nsample_dict[ap][tnum], dtype=dtype_opt)
                fg_r.append(foreground_coeffs[ap][tnum].real.squeeze() / rmsdata)
                fg_i.append(isign * foreground_coeffs[ap][tnum].imag.squeeze() / rmsdata)
            # initialize fg_coeffs to optimize
            fg_r = tf.Variable(tf.convert_to_tensor(np.hstack(fg_r), dtype=dtype_opt))
            fg_i = tf.Variable(tf.convert_to_tensor(np.hstack(fg_i), dtype=dtype_opt))
            g_r = tf.Variable(tf.convert_to_tensor(np.vstack([gain_dict[ant_map[i]][tnum].real for i in ant_map.keys()]), dtype=dtype_opt))
            g_i = tf.Variable(tf.convert_to_tensor(np.vstack([gain_dict[ant_map[i]][tnum].imag for i in ant_map.keys()]), dtype=dtype_opt))
            # get the foreground model.
            def yield_fg_model(i, j):
                vr = tf.reduce_sum(evec_map[i, j] * fg_r[fgrange_map[i, j][0]:fgrange_map[i, j][1]], axis=1) # real part of fg model.
                vi = tf.reduce_sum(evec_map[i, j] * fg_i[fgrange_map[i, j][0]:fgrange_map[i, j][1]], axis=1) # imag part of fg model.
                return vr, vi
            # get the calibrated model
            def yield_model(i, j):
                vr, vi = yield_fg_model(i, j)
                model_r = (g_r[i] * g_r[j] + g_i[i] * g_i[j]) *  vr + (g_r[i] * g_i[j] - g_i[i] * g_r[j]) * vi # real part of model with gains
                model_i = (g_r[i] * g_r[j] + g_i[i] * g_i[j]) * vi + (g_i[i] * g_r[j] - g_r[i] * g_i[j]) * vr # imag part of model with gains
                return model_r, model_i
            # function for computing loss to be minimized in optimization.
            # tf.function decorator -> will pre-optimize computation in graph mode.
            # lets us side-step hard-to-read and sometimes wasteful purely
            # parallelpiped tensor computations. This leads to a x4 speedup
            # in processing the data in test_calibrate_and_model_dpss
            # on an i5 CPU macbook over pure python. Not sure how this scales
            # with array size.
            # see https://www.tensorflow.org/guide/function.
            @tf.function
            def cal_loss():
                loss_total = 0.
                wsum = 0.
                for i, j in data_map_r.keys():
                    model_r, model_i = yield_model(i, j)
                    loss_total += tf.reduce_sum(tf.square(model_r  - data_map_r[i, j]) * weights_map[i, j])
                    # imag part of loss
                    loss_total += tf.reduce_sum(tf.square(model_i - data_map_i[i, j]) * weights_map[i, j])
                    wsum += 2 * tf.reduce_sum(weights_map[i, j])
                return loss_total / wsum
            # initialize the optimizer.
            opt = OPTIMIZERS[optimizer](**opt_kwargs)
            # set up history recording
            fitting_info_t = {'loss_history':[]}
            if record_var_history:
                fitting_info_t['g_r'] = []
                fitting_info_t['g_i'] = []
                if not freeze_model:
                    fitting_info_t['fg_r'] = []
                    fitting_info_t['fg_i'] = []
            # perform optimization loop.
            for step in tqdm.tqdm(range(maxsteps)):
                with tf.GradientTape() as tape:
                    loss = cal_loss()
                if freeze_model:
                    vars = [g_r, g_i]
                else:
                    vars = [g_r, g_i, fg_r, fg_i]
                grads = tape.gradient(loss, vars)
                opt.apply_gradients(zip(grads, vars))
                fitting_info_t['loss_history'].append(loss.numpy())
                if record_var_history:
                    fitting_info_t['g_r'].append(g_r.numpy())
                    fitting_info_t['g_i'].append(g_i.numpy())
                    if not freeze_model:
                        fitting_info_t['fg_r'].append(fg_r.numpy())
                        fitting_info_t['fg_i'].append(fg_i.numpy())
                if step >= 1 and np.abs(fitting_info_t['loss_history'][-1] - fitting_info_t['loss_history'][-2]) < tol:
                    break

            ants_visited = set({})
            # insert calibrated / model subtracted data / gains
            # into antenna keyed dictionaries.
            for ap in data_dict:
                i, j = ant_mapi[ap[0]], ant_mapi[ap[1]]
                apg = (ap[0], ap[1])
                if i > j:
                    i, j = j, i
                    isign = -1.
                    apg = apg[::-1]
                model_r, model_i = yield_model(i, j)
                model_fg_r, model_fg_i = yield_fg_model(i, j)

                if apg[0] not in ants_visited:
                    gain_dict[apg[0]] = g_r[i].numpy() + 1j * g_i[i].numpy()
                    ants_visited.add(apg[0])
                if apg[1] not in ants_visited:
                    gain_dict[apg[1]] = g_r[j].numpy() + 1j * g_i[j].numpy()
                    ants_visited.add(apg[1])

                model_cal = model_r.numpy() + isign * 1j * model_i.numpy()
                model_fg = model_fg_r.numpy() + isign * 1j * model_fg_i.numpy()


                # set foreground model. Flux scale and overall phase will be off.
                # we will fix this at the end.
                model_dict[ap][tnum] = model_fg * rmsdata
                # subtract model
                resid_dict[ap][tnum] -= model_cal * rmsdata
                # divide by cal solution flux and phase scales will be off. We will fix this at the end.
                resid_dict[ap][tnum] = resid_dict[ap][tnum] / (gain_dict[ap[0]][tnum] * np.conj(gain_dict[ap[1]][tnum]))

            fitting_info_p[tnum] = fitting_info_t

        # now transfer antenna keyed dictionaries to uvdata/uvcal objects for
        # i/o
        ants_visited = set({})
        for ap in resid_dict:
            dinds = resid.antpair2ind(ap)
            resid.data_array[dinds, 0, :, polnum] = resid_dict[ap]
            model.data_array[dinds, 0, :, polnum] = model_dict[ap]
            model.flag_array[dinds, 0, :, polnum] = np.zeros_like(model.flag_array[dinds, 0, :, polnum])
            filtered.data_array[dinds, 0, :, polnum] = resid.data_array[dinds, 0, :, polnum] + model.data_array[dinds, 0, :, polnum]
            filtered.flag_array[dinds, 0, :, polnum] = model.flag_array[dinds, 0, :, polnum]
            if ap[0] not in ants_visited:
                gind = gains.ant2ind(ap[0])
                gains.gain_array[gind, 0, :, :, polnum] = gain_dict[ap[0]].T
                ants_visited.add(ap[0])
            if ap[1] not in ants_visited:
                gind = gains.ant2ind(ap[1])
                gains.gain_array[gind, 0, :, :, polnum] = gain_dict[ap[1]].T
        # free up memory
        del resid_dict, model_dict, data_dict, gain_dict
        # rescale by the abs rms of the data and an overall phase.
        scale_factor_phase = np.angle(np.mean(sky_model.data_array[:, :, :, polnum][~uvdata.flag_array[:, :, :, polnum]] / model.data_array[:, :, :, polnum][~uvdata.flag_array[:, :, :, polnum]]))
        scale_factor_abs = np.sqrt(np.mean(np.abs(sky_model.data_array[:, :, :, polnum][~uvdata.flag_array[:, :, :, polnum]] / model.data_array[:, :, :, polnum][~uvdata.flag_array[:, :, :, polnum]]) ** 2.))
        scale_factor = scale_factor_abs * np.exp(1j * scale_factor_phase)
        model.data_array[:, :, :, polnum] *= scale_factor
        resid.data_array[:, :, :, polnum] *= scale_factor
        filtered.data_array[:, :, :, polnum] *= scale_factor
        gains.gain_array[:, :, :, polnum]  = gains.gain_array[:, :, :, polnum] / np.sqrt(scale_factor)

    return model, resid, filtered, gains, fitting_info


def calibrate_and_model_dpss(uvdata, horizon=1., min_dly=0., offset=0., **fitting_kwargs):
    """Simultaneously solve for gains and model foregrounds with DPSS vectors.

    Parameters
    ----------
    uvdata: UVData object.
        dataset to calibrate and filter.
    horizon: float, optional
        fraction of baseline delay length to model with dpss modes
        unitless.
        default is 1.
    min_dly: float, optional
        minimum delay to model with dpss models.
        in units of ns.
        default is 0.
    offset: float optional
        offset off of horizon wedge to include in dpss delay range.
        in units of ns.
        default is 0.
    fitting_kwargs: kwarg dict
        additional kwargs for calibrate_and_model_per_baseline.
        see docstring of calibrate_and_model_per_baseline.

    Returns
    -------
    model: UVData object
        uvdata object containing DPSS model of intrinsic foregrounds.
    resid: UVData object
        uvdata object containing residuals after subtracting model times gains and applying gains.
    filtered: UVData object
        uvdata object containing the sum of the residuals and instrinsic foreground model.
    gains: UVCal object
        uvcal object containing fitted gains.
    fitting_info:
        dictionary containing fit history for each time-step and polarization in the data with fields:
        'loss_history': list of values of the loss function in each minimization iteration.
        if record_var_history is True:
            each of the following fields is included which points to an Nstep x Nvar array of values
            'fg_r': real part of foreground coefficients
            'fg_i': imag part of foreground coefficients
            'g_r': real part of gains.
            'g_i': imag part of gains
    """
    dpss_evecs = {}
    # generate dpss modeling vectors.
    for ap in uvdata.get_antpairs():
        i = np.argmin(np.abs(ap[0] - uvdata.antenna_numbers))
        j = np.argmin(np.abs(ap[1] - uvdata.antenna_numbers))
        dly = np.linalg.norm(uvdata.antenna_positions[i] - uvdata.antenna_positions[j]) / .3
        dly = max(min_dly, dly * horizon + offset) / 1e9
        dpss_evecs[ap] = dspec.dpss_operator(uvdata.freq_array[0], filter_centers=[0.0], filter_half_widths=[dly], eigenval_cutoff=[1e-12])[0]
    model, resid, filtered, gains, fitted_info = calibrate_and_model_per_baseline(uvdata=uvdata, foreground_basis_vectors=dpss_evecs, **fitting_kwargs)
    return model, resid, filtered, gains, fitted_info


def read_calibrate_and_model_per_baseline(infilename, incalfilename=None, refmodelname=None, residfilename=None,
                                          modelfilename=None, filteredfilename=None, calfilename=None, modeling_basis='dpss',
                                          clobber=False, **cal_kwargs):
    """Driver

    Parameters
    ----------
    infilename: str
        path to the input uvh5 data file with data to calibrate and filter.
    incalefilename: str, optional
        path to input calfits calibration file to use as a starting point for gain solutions.
    refmodelname: str, optional
        path to an optional reference sky model that can be used to set initial gains and foreground coefficients.
        default is None -> initial foreground coeffs set by
    residfilename: str, optional
        path to file to output uvh5 file that stores the calibrated residual file.
        default is None -> no resid file will be written.
    modelfilename: str, optional
        path to output uvh5 file that stores the intrinsic foreground model.
        default is None -> no modelfile will be writen.
    filterfilename: str, optional
        path to output uvh5 file that stores residual plus foreground model, this is
        the fully calibrated data that includes both a calibrated foreground model
        and residual.
        default is None -> no filterfile will be written.
    calfilename: str, optional
        path to output calfits file to write gain estimates too.
    modeling basis: str, optional
        string specifying the per-baseline basis functions to use for modeling foregrounds.
        default is 'dpss'. Currently, only 'dpss' is supported.
    clobber: bool, optional
        overwrite existing output files.
        default is False.
    cal_kwargs: kwarg_dict.
        kwargs for calibrate_data_model_dpss and calibrate_and_model_per_baseline
        see the docstrings of these functions for more details.
    """
    # initialize uvdata
    uvdata = UVData()
    uvdata.read_uvh5(infilename)
    # initalize input calibration
    if incalfilename is not None:
        gains = UVCal()
        gains.read_calfits(incalfilename)
    else:
        gains = None
    if refmodelname is not None:
        sky_model = UVData()
        sky_model.read_uvh5(refmodelname)
    else:
        sky_model=None
    if modeling_basis == 'dpss':
        model, resid, filtered, gains, fitted_info = calibrate_and_model_dpss(uvdata=uvdata, sky_model=sky_model, gains=gains,
                                                                              **cal_kwargs)
    else:
        raise NotImplementedError("only 'dpss' modeling basis is implemented.")
    if residfilename is not None:
        resid.write_uvh5(residfilename, clobber=clobber)
    if modelfilename is not None:
        model.write_uvh5(modelfilename, clobber=clobber)
    if filteredfilename is not None:
        filtered.write_uvh5(filteredfilename, clobber=clobber)
    if calfilename is not None:
        gains.write_calfits(calfilename, clobber=clobber)
    return model, resid, filtered, gains, fitted_info





def red_calibrate_and_model_dpss_argparser():
    """Get argparser for calibrating and filtering.

    Parameters
    ----------
    N/A

    Returns
    -------
    ap: argparse.ArgumentParser object.
        parser for running read_calibrate_and_filter_data_per_baseline with modeling_basis='dpss'

    """
    ap = argparse.ArgumentParser(description="Simultaneous Gain Calibration and Filtering of Foregrounds using DPSS modes")
    io_opts = ap.add_argument_group(title="I/O options.")
    io_opts.add_argument("infilename", type=str, help="Path to data file to be calibrated and modeled.")
    io_opts.add_argument("--incalfilename", type=str, help="Path to optional initial gain files.", default=None)
    io_opts.add_argument("--refmodelname", type=str, help="Path to a reference sky model that can be used to initialize foreground coefficients and set overall flux scale and phase.")
    io_opts.add_argument("--residfilename", type=str, help="Path to write output uvh5 residual.", default=None)
    io_opts.add_argument("--modelfilename", type=str, help="Path to write output uvh5 model.", default=None)
    io_opts.add_argument("--filteredfilename", type=str, help="Path to write output uvh5 filtered and calibrated data.")
    io_opts.add_argument("--calfilename", type=str, help="path to write output calibration gains.")
    fg_opts = ap.add_argument_group(title="Options for foreground modeling.")
    fg_opts.add_argument("--horizon", type=float, default=1.0, help="Fraction of horizon delay to model with DPSS modes.")
    fg_opts.add_argument("--offset", type=float, default=0.0, help="Offset off of horizon delay (in ns) to model foregrounds with DPSS modes.")
    fg_opts.add_argument("--min_dly", type=float, default=0.0, help="minimum delay, regardless of baseline length, to model foregrounds with DPSS modes.")
    fit_opts = ap.add_argument_group(title="Options for fitting and optimization")
    fit_opts.add_argument("--freeze_model", default=False, action="store_true", help="Only optimize gains (freeze foreground model on existing data ore user provided sky-model file).")
    fit_opts.add_argument("--optimizer", default="Adamax", type=str, help="Optimizer to use in gradient descent. See https://www.tensorflow.org/api_docs/python/tf/keras/optimizers")
    fit_opts.add_argument("--tol", type=float, default=1e-14, help="Halt optimization if loss (chsq / ndata) changes by less then this amount.")
    fit_opts.add_argument("--maxsteps", type=int, default=10000, help="Maximum number of steps to carry out optimization to")
    fit_opts.add_argument("--verbose", default=False, action="store_true", help="Lots of outputs.")
    fit_opts.add_argument("--learning_rate", default=1e-2, type=float, help="Initial learning rate for optimization")
    return ap
