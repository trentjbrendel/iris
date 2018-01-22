"""Solve for aberrations on the optical axis given some truth MTF values and lens parameters."""
import os
import time
from functools import partial
from multiprocessing import Pool

import numpy as np

from scipy.optimize import minimize

from prysm import FringeZernike, Seidel, MTF
from prysm.thinlens import image_displacement_to_defocus
from prysm.mtf_utils import mtf_ts_extractor
from prysm.otf import diffraction_limited_mtf

from pyphase.util import mtf_cost_fcn, net_costfcn_reducer, parse_cost_by_iter_lbfgsb
from pyphase.forcefully_redirect_stdout import forcefully_redirect_stdout


def grab_axial_data(setup_parameters, truth_dataframe):
    """Pull axial through-focus MTF data from a pandas DataFrame.

    Parameters
    ----------
    setup_parameters : dict
        dictionary with keys `fno` and `wavelength`
    truth_dataframe : pandas.DataFrame
        Dataframe with columns `Field`, `Focus`, `Azimuth`, and `MTF`.

    Returns
    -------
    wvfront_defocus : numpy.ndarray
        array of defocus values in waves zero to peak.
    ax_t : numpy.ndarray
        array of tangential MTF values.
    ax_s : numpy.ndarray
        array of sagittal MTF values.

    """
    s = setup_parameters
    axial_mtf_data = truth_dataframe[truth_dataframe.Field == 0]
    focuspos = np.unique(axial_mtf_data.Focus.as_matrix())
    wvfront_defocus = image_displacement_to_defocus(focuspos, s['fno'], s['wavelength'])
    ax_t = []
    ax_s = []
    for pos in focuspos:
        fd = axial_mtf_data[axial_mtf_data.Focus == pos]
        ax_t.append(fd[fd.Azimuth == 'Tan']['MTF'].as_matrix())
        ax_s.append(fd[fd.Azimuth == 'Sag']['MTF'].as_matrix())

    wvfront_defocus = np.asarray(wvfront_defocus)
    ax_t = np.asarray(ax_t)
    ax_s = np.asarray(ax_s)
    return wvfront_defocus, ax_t, ax_s


def realize_focus_plane(base_wavefront, t_true, s_true, defocus_wavefront):
    """Compute the cost function for a single focal plane.

    Parameters
    ----------
    base_wavefront : prysm.Pupil
        a prysm Pupil object.
    t_true : numpy.ndarray
        array of true MTF values.
    s_true : numpy.ndarray
        array of true MTF values.
    defocus_wavefront : Pupil
        a prysm Pupil object.

    Returns
    -------
    float
        value of the cost function for this focus plane realization.

    """
    global setup_parameters, diffraction
    prop_wvfront = base_wavefront + defocus_wavefront
    mtf = MTF.from_pupil(prop_wvfront, setup_parameters['efl'])
    t, s = mtf_ts_extractor(mtf, setup_parameters['freqs'])
    t, s = t / diffraction, s / diffraction
    return mtf_cost_fcn(t_true / diffraction, s_true / diffraction, t, s)


def optfcn(wavefrontcoefs):
    """Optimization routine used to compare simulation data to measurement data.

    Parameters
    ----------
    wavefrontcoefs : iterable
        a vector of wavefront coefficients.

    Returns
    -------
    float
        cost function value

    """
    # generate a "base pupil" with some aberration content
    global setup_parameters, decoder_ring, pool, t_true, s_true, defocus_pupils
    s = setup_parameters
    efl, fno, wavelength, samples = s['efl'], s['fno'], s['wavelength'], s['samples']
    pupil_pass_zernikes = {key: value for (key, value) in zip(decoder_ring.values(), wavefrontcoefs)}
    pupil = FringeZernike(**pupil_pass_zernikes, base=1,
                          epd=efl / fno, wavelength=wavelength, samples=samples)

    # for each focus plane, compute the cost function
    rfp_mp = partial(realize_focus_plane, pupil)
    costfcn = pool.starmap(rfp_mp, zip(t_true, s_true, defocus_pupils))
    return net_costfcn_reducer(costfcn)


def ready_pool(arg_dict):
    """Initialize global variables inside process pool for windows support of shared read-only global state.

    Parameters
    ----------
    arg_dict : dict
        dictionary of key/value pairs of variable names and values to expose at the global level.

    """
    globals().update(arg_dict)


def sph_from_focusdiverse_axial_mtf(sys_parameters, truth_dataframe, guess=(0, 0, 0, 0)):
    """Retrieve spherical aberration-related coefficients from axial MTF data.

    Parameters
    ----------
    sys_parameters : dict
        dictionary with keys `efl`, `fno`, `wavelength`, `samples`.
    truth_dataframe : pandas.DataFrame
        a dataframe containing truth values.
    guess : iterable, optional
        guess coefficients for the wavefront.

    Returns
    -------
    TYPE
        Description

    Raises
    ------
    Exception
        Any exceptions raised by called functions are re-raised by this function.

    """
    # declare some state for this run as global variables to speed up access in multiprocess pool
    global t_true, s_true, setup_parameters, decoder_ring, defocus_pupils, diffraction, pool
    setup_parameters = sys_parameters
    (focus_diversity,
     ax_t, ax_s) = grab_axial_data(setup_parameters, truth_dataframe)

    # casting ndarray to list makes it a list of arrays where the first index
    # is the focal plane and the second frequency.
    t_true, s_true = list(ax_t), list(ax_s)

    # precompute the defocus wavefronts to accelerate solving
    s = setup_parameters

    efl, fno, wvl, freqs, samples = s['efl'], s['fno'], s['wavelength'], s['freqs'], s['samples']
    diffraction = diffraction_limited_mtf(fno, wvl, frequencies=freqs)
    defocus_pupils = []
    for focus in focus_diversity:
        defocus_pupils.append(Seidel(W020=focus, epd=efl / fno, wavelength=wvl, samples=samples))

    decoder_ring = {
        0: 'Z4',
        1: 'Z9',
        2: 'Z16',
        3: 'Z25',
    }

    _globals = {
        't_true': t_true,
        's_true': s_true,
        'setup_parameters': setup_parameters,
        'decoder_ring': decoder_ring,
        'defocus_pupils': defocus_pupils,
        'diffraction': diffraction,
    }
    pool = Pool(processes=os.cpu_count() - 1, initializer=ready_pool, initargs=[_globals])
    optimizer_function = optfcn
    parameter_vectors = []

    def callback(x):
        parameter_vectors.append(x)

    try:
        t_start = time.perf_counter()
        # do the optimization and capture the per-iteration information from stdout
        with forcefully_redirect_stdout() as out:
            result = minimize(
                fun=optimizer_function,
                x0=guess,
                method='L-BFGS-B',
                options={
                    'disp': True,
                    'gtol': 1e-10,
                    'ftol': 1e-12,
                },
                callback=callback)

        t_end = time.perf_counter()
        # grab the extra data
        cost_by_iter = parse_cost_by_iter_lbfgsb(out['txt'])

        # add the guess to the front of the parameter vectors
        # cost_init = optfcn_seq(setup_parameters, focus_diversity, ax_t, ax_s, guess)
        parameter_vectors.insert(0, np.asarray(guess))
        # cost_by_iter.insert(0, cost_init)
        result.x_iter = parameter_vectors
        result.fun_iter = cost_by_iter
        result.time = t_end - t_start
        pool.close()
        pool.join()
        return result
    except Exception as e:
        pool.close()
        pool.join()
        raise e
