"""Main recipe."""
import os
import time
from multiprocessing import Pool

import numpy as np
from scipy.optimize import minimize

from iris.core import prepare_globals, optfcn
from iris.forcefully_redirect_stdout import forcefully_redirect_stdout
from iris.utilities import parse_cost_by_iter_lbfgsb
from iris.recipes.axis import grab_axial_data

from prysm.otf import diffraction_limited_mtf


def opt_routine(sys_parameters, truth_dataframe, codex, guess=(0, 0, 0, 0),
                parallel=False, core_opts=None):
    """Retrieve spherical aberration-related coefficients from axial MTF data.

    Parameters
    ----------
    sys_parameters : `dict`
        dictionary with keys efl, fno, wavelength, samples, focus_planes, focus_range_waves, freqs, freq_step
    truth_dataframe : `pandas.DataFrame`
        a dataframe containing truth values
    codex : dict
        dictionary of key, value pairs where keys are ints and values are strings.  Maps parameter
        numbers to zernike numbers, e.g. {0: 'Z1', 1: 'Z9'} maps (10, 11) to {'Z1': 10, 'Z9': 11}
    guess : iterable, optional
        guess coefficients for the wavefront
    parallel : `bool`, optional
        whether to run optimization in parallel.  Defaults to true
    core_otps: `tuple` or None, optional
        options to pass to the optimizaiton core

    Returns
    -------
    `dict`
        dictionary with keys, types:
            - sim_params, dict
            - codex, dict
            - truth_params, tuple
            - truth_rmswfe, float
            - zernike_norm, bool
            - result_final, tuple
            - result_iter, list
            - cost_final, float
            - cost_iter, list
            - time, float

    """
    # declare some state for this run as global variables to speed up access in multiprocess pool
    setup_parameters = sys_parameters
    (focus_diversity,
     ax_t, ax_s) = grab_axial_data(setup_parameters, truth_dataframe)

    # casting ndarray to list makes it a list of arrays where the first index
    # is the focal plane and the second frequency.
    t_true, s_true = list(ax_t), list(ax_s)

    # now compute diffraction for the setup and use it as a normailzation
    diffraction = diffraction_limited_mtf(setup_parameters.fno, setup_parameters.wvl, frequencies=setup_parameters.freqs)

    _globals = {
        't_true': t_true,
        's_true': s_true,
        'defocus': focus_diversity,
        'setup_parameters': setup_parameters,
        'decoder_ring': codex,
        'diffraction': diffraction,
    }
    if parallel is True:
        pool = Pool(processes=os.cpu_count() - 1, initializer=prepare_globals, initargs=[_globals])
    else:
        pool = None
    prepare_globals({**_globals, 'pool': pool})
    optimizer_function = optfcn
    parameter_vectors = []

    def callback(x):
        parameter_vectors.append(x.copy())

    try:
        parameter_vectors.append(np.asarray(guess))
        t_start = time.perf_counter()
        # do the optimization and capture the per-iteration information from stdout
        with forcefully_redirect_stdout() as out:
            if core_opts is None:
                args = (None, None)
            result = minimize(
                fun=optimizer_function,
                x0=guess,
                method='L-BFGS-B',
                options={
                    'disp': True,
                    'ftol': 1e-2,
                },
                args=args,
                callback=callback)

        t_end = time.perf_counter()
        # grab the extra data and put everything on the optimizationresult
        cost_by_iter = parse_cost_by_iter_lbfgsb(out['txt'])
        result.x_iter = parameter_vectors
        result.fun_iter = cost_by_iter
        result.time = t_end - t_start
        return result
    finally:
        if pool is not None:
            pool.close()
            pool.join()
