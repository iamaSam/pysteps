"""
pysteps.nowcasts.steps
======================

Implementation of the STEPS stochastic nowcasting method as described in
:cite:`Seed2003`, :cite:`BPS2006` and :cite:`SPN2013`.

.. autosummary::
    :toctree: ../generated/

    forecast
"""

import numpy as np
import scipy.ndimage
import time

from pysteps import cascade
from pysteps import extrapolation
from pysteps import noise
from pysteps import utils
from pysteps.nowcasts import utils as nowcast_utils
from pysteps.postprocessing import probmatching
from pysteps.timeseries import autoregression, correlation
from pysteps.nowcasts.utils import compute_percentile_mask

try:
    import dask

    DASK_IMPORTED = True
except ImportError:
    DASK_IMPORTED = False


def forecast(
    precip,
    velocity,
    timesteps,
    n_ens_members=24,
    n_cascade_levels=6,
    precip_thr=None,
    kmperpixel=None,
    timestep=None,
    extrap_method="semilagrangian",
    decomp_method="fft",
    bandpass_filter_method="gaussian",
    noise_method="nonparametric",
    noise_stddev_adj=None,
    ar_order=2,
    vel_pert_method="bps",
    conditional=False,
    probmatching_method="cdf",
    mask_method="incremental",
    callback=None,
    return_output=True,
    seed=None,
    num_workers=1,
    fft_method="numpy",
    domain="spatial",
    extrap_kwargs=None,
    filter_kwargs=None,
    noise_kwargs=None,
    vel_pert_kwargs=None,
    mask_kwargs=None,
    measure_time=False,
):
    """Generate a nowcast ensemble by using the Short-Term Ensemble Prediction
    System (STEPS) method.

    Parameters
    ----------
    precip: array-like
        Array of shape (ar_order+1,m,n) containing the input precipitation fields
        ordered by timestamp from oldest to newest. The time steps between the
        inputs are assumed to be regular.
    velocity: array-like
        Array of shape (2,m,n) containing the x- and y-components of the advection
        field. The velocities are assumed to represent one time step between the
        inputs. All values are required to be finite.
    timesteps: int or list of floats
        Number of time steps to forecast or a list of time steps for which the
        forecasts are computed (relative to the input time step). The elements
        of the list are required to be in ascending order.
    n_ens_members: int, optional
        The number of ensemble members to generate.
    n_cascade_levels: int, optional
        The number of cascade levels to use.
    precip_thr: float, optional
        Specifies the threshold value for minimum observable precipitation
        intensity. Required if mask_method is not None or conditional is True.
    kmperpixel: float, optional
        Spatial resolution of the input data (kilometers/pixel). Required if
        vel_pert_method is not None or mask_method is 'incremental'.
    timestep: float, optional
        Time step of the motion vectors (minutes). Required if vel_pert_method is
        not None or mask_method is 'incremental'.
    extrap_method: str, optional
        Name of the extrapolation method to use. See the documentation of
        pysteps.extrapolation.interface.
    decomp_method: {'fft'}, optional
        Name of the cascade decomposition method to use. See the documentation
        of pysteps.cascade.interface.
    bandpass_filter_method: {'gaussian', 'uniform'}, optional
        Name of the bandpass filter method to use with the cascade decomposition.
        See the documentation of pysteps.cascade.interface.
    noise_method: {'parametric','nonparametric','ssft','nested',None}, optional
        Name of the noise generator to use for perturbating the precipitation
        field. See the documentation of pysteps.noise.interface. If set to None,
        no noise is generated.
    noise_stddev_adj: {'auto','fixed',None}, optional
        Optional adjustment for the standard deviations of the noise fields added
        to each cascade level. This is done to compensate incorrect std. dev.
        estimates of casace levels due to presence of no-rain areas. 'auto'=use
        the method implemented in pysteps.noise.utils.compute_noise_stddev_adjs.
        'fixed'= use the formula given in :cite:`BPS2006` (eq. 6), None=disable
        noise std. dev adjustment.
    ar_order: int, optional
        The order of the autoregressive model to use. Must be >= 1.
    vel_pert_method: {'bps',None}, optional
        Name of the noise generator to use for perturbing the advection field. See
        the documentation of pysteps.noise.interface. If set to None, the advection
        field is not perturbed.
    conditional: bool, optional
        If set to True, compute the statistics of the precipitation field
        conditionally by excluding pixels where the values are below the
        threshold precip_thr.
    mask_method: {'obs','sprog','incremental',None}, optional
        The method to use for masking no precipitation areas in the forecast
        field. The masked pixels are set to the minimum value of the observations.
        'obs' = apply precip_thr to the most recently observed precipitation
        intensity field, 'sprog' = use the smoothed forecast field from S-PROG,
        where the AR(p) model has been applied, 'incremental' = iteratively
        buffer the mask with a certain rate (currently it is 1 km/min),
        None=no masking.
    probmatching_method: {'cdf','mean',None}, optional
        Method for matching the statistics of the forecast field with those of
        the most recently observed one. 'cdf'=map the forecast CDF to the observed
        one, 'mean'=adjust only the conditional mean value of the forecast field
        in precipitation areas, None=no matching applied. Using 'mean' requires
        that mask_method is not None.
    callback: function, optional
        Optional function that is called after computation of each time step of
        the nowcast. The function takes one argument: a three-dimensional array
        of shape (n_ens_members,h,w), where h and w are the height and width
        of the input field R, respectively. This can be used, for instance,
        writing the outputs into files.
    return_output: bool, optional
        Set to False to disable returning the outputs as numpy arrays. This can
        save memory if the intermediate results are written to output files using
        the callback function.
    seed: int, optional
        Optional seed number for the random generators.
    num_workers: int, optional
        The number of workers to use for parallel computation. Applicable if dask
        is enabled or pyFFTW is used for computing the FFT. When num_workers>1, it
        is advisable to disable OpenMP by setting the environment variable
        OMP_NUM_THREADS to 1. This avoids slowdown caused by too many simultaneous
        threads.
    fft_method: str, optional
        A string defining the FFT method to use (see utils.fft.get_method).
        Defaults to 'numpy' for compatibility reasons. If pyFFTW is installed,
        the recommended method is 'pyfftw'.
    domain: {"spatial", "spectral"}
        If "spatial", all computations are done in the spatial domain (the
        classical STEPS model). If "spectral", the AR(2) models and stochastic
        perturbations are applied directly in the spectral domain to reduce
        memory footprint and improve performance :cite:`PCH2019b`.
    extrap_kwargs: dict, optional
        Optional dictionary containing keyword arguments for the extrapolation
        method. See the documentation of pysteps.extrapolation.
    filter_kwargs: dict, optional
        Optional dictionary containing keyword arguments for the filter method.
        See the documentation of pysteps.cascade.bandpass_filters.py.
    noise_kwargs: dict, optional
        Optional dictionary containing keyword arguments for the initializer of
        the noise generator. See the documentation of pysteps.noise.fftgenerators.
    vel_pert_kwargs: dict, optional
        Optional dictionary containing keyword arguments 'p_par' and 'p_perp' for
        the initializer of the velocity perturbator. The choice of the optimal
        parameters depends on the domain and the used optical flow method.

        Default parameters from :cite:`BPS2006`:
        p_par  = [10.88, 0.23, -7.68]
        p_perp = [5.76, 0.31, -2.72]

        Parameters fitted to the data (optical flow/domain):

        darts/fmi:
        p_par  = [13.71259667, 0.15658963, -16.24368207]
        p_perp = [8.26550355, 0.17820458, -9.54107834]

        darts/mch:
        p_par  = [24.27562298, 0.11297186, -27.30087471]
        p_perp = [-7.80797846e+01, -3.38641048e-02, 7.56715304e+01]

        darts/fmi+mch:
        p_par  = [16.55447057, 0.14160448, -19.24613059]
        p_perp = [14.75343395, 0.11785398, -16.26151612]

        lucaskanade/fmi:
        p_par  = [2.20837526, 0.33887032, -2.48995355]
        p_perp = [2.21722634, 0.32359621, -2.57402761]

        lucaskanade/mch:
        p_par  = [2.56338484, 0.3330941, -2.99714349]
        p_perp = [1.31204508, 0.3578426, -1.02499891]

        lucaskanade/fmi+mch:
        p_par  = [2.31970635, 0.33734287, -2.64972861]
        p_perp = [1.90769947, 0.33446594, -2.06603662]

        vet/fmi:
        p_par  = [0.25337388, 0.67542291, 11.04895538]
        p_perp = [0.02432118, 0.99613295, 7.40146505]

        vet/mch:
        p_par  = [0.5075159, 0.53895212, 7.90331791]
        p_perp = [0.68025501, 0.41761289, 4.73793581]

        vet/fmi+mch:
        p_par  = [0.29495222, 0.62429207, 8.6804131 ]
        p_perp = [0.23127377, 0.59010281, 5.98180004]

        fmi=Finland, mch=Switzerland, fmi+mch=both pooled into the same data set

        The above parameters have been fitten by using run_vel_pert_analysis.py
        and fit_vel_pert_params.py located in the scripts directory.

        See pysteps.noise.motion for additional documentation.
    mask_kwargs: dict
        Optional dictionary containing mask keyword arguments 'mask_f' and
        'mask_rim', the factor defining the the mask increment and the rim size,
        respectively.
        The mask increment is defined as mask_f*timestep/kmperpixel.
    measure_time: bool
        If set to True, measure, print and return the computation time.

    Returns
    -------
    out: ndarray
        If return_output is True, a four-dimensional array of shape
        (n_ens_members,num_timesteps,m,n) containing a time series of forecast
        precipitation fields for each ensemble member. Otherwise, a None value
        is returned. The time series starts from t0+timestep, where timestep is
        taken from the input precipitation fields R. If measure_time is True, the
        return value is a three-element tuple containing the nowcast array, the
        initialization time of the nowcast generator and the time used in the
        main loop (seconds).

    See also
    --------
    pysteps.extrapolation.interface, pysteps.cascade.interface,
    pysteps.noise.interface, pysteps.noise.utils.compute_noise_stddev_adjs

    References
    ----------
    :cite:`Seed2003`, :cite:`BPS2006`, :cite:`SPN2013`, :cite:`PCH2019b`
    """

    _check_inputs(precip, velocity, timesteps, ar_order)

    if extrap_kwargs is None:
        extrap_kwargs = dict()

    if filter_kwargs is None:
        filter_kwargs = dict()

    if noise_kwargs is None:
        noise_kwargs = dict()

    if vel_pert_kwargs is None:
        vel_pert_kwargs = dict()

    if mask_kwargs is None:
        mask_kwargs = dict()

    if np.any(~np.isfinite(velocity)):
        raise ValueError("velocity contains non-finite values")

    if mask_method not in ["obs", "sprog", "incremental", None]:
        raise ValueError(
            "unknown mask method %s: must be 'obs', 'sprog' or 'incremental' or None"
            % mask_method
        )

    if conditional and precip_thr is None:
        raise ValueError("conditional=True but precip_thr is not set")

    if mask_method is not None and precip_thr is None:
        raise ValueError("mask_method is not None but precip_thr is not set")

    if noise_stddev_adj not in ["auto", "fixed", None]:
        raise ValueError(
            "unknown noise_std_dev_adj method %s: must be 'auto', 'fixed', or None"
            % noise_stddev_adj
        )

    if kmperpixel is None:
        if vel_pert_method is not None:
            raise ValueError("vel_pert_method is set but kmperpixel=None")
        if mask_method == "incremental":
            raise ValueError("mask_method='incremental' but kmperpixel=None")

    if timestep is None:
        if vel_pert_method is not None:
            raise ValueError("vel_pert_method is set but timestep=None")
        if mask_method == "incremental":
            raise ValueError("mask_method='incremental' but timestep=None")

    print("Computing STEPS nowcast")
    print("-----------------------")
    print("")

    print("Inputs")
    print("------")
    print(f"input dimensions: {precip.shape[1]}x{precip.shape[2]}")
    if kmperpixel is not None:
        print(f"km/pixel:         {kmperpixel}")
    if timestep is not None:
        print(f"time step:        {timestep} minutes")
    print("")

    print("Methods")
    print("-------")
    print(f"extrapolation:          {extrap_method}")
    print(f"bandpass filter:        {bandpass_filter_method}")
    print(f"decomposition:          {decomp_method}")
    print(f"noise generator:        {noise_method}")
    print("noise adjustment:       {}".format(("yes" if noise_stddev_adj else "no")))
    print(f"velocity perturbator:   {vel_pert_method}")
    print("conditional statistics: {}".format(("yes" if conditional else "no")))
    print(f"precip. mask method:    {mask_method}")
    print(f"probability matching:   {probmatching_method}")
    print(f"FFT method:             {fft_method}")
    print(f"domain:                 {domain}")
    print("")

    print("Parameters")
    print("----------")
    if isinstance(timesteps, int):
        print(f"number of time steps:     {timesteps}")
    else:
        print(f"time steps:               {timesteps}")
    print(f"ensemble size:            {n_ens_members}")
    print(f"parallel threads:         {num_workers}")
    print(f"number of cascade levels: {n_cascade_levels}")
    print(f"order of the AR(p) model: {ar_order}")
    if vel_pert_method == "bps":
        vp_par = vel_pert_kwargs.get("p_par", noise.motion.get_default_params_bps_par())
        vp_perp = vel_pert_kwargs.get(
            "p_perp", noise.motion.get_default_params_bps_perp()
        )
        print(
            f"velocity perturbations, parallel:      {vp_par[0]},{vp_par[1]},{vp_par[2]}"
        )
        print(
            f"velocity perturbations, perpendicular: {vp_perp[0]},{vp_perp[1]},{vp_perp[2]}"
        )

    if conditional or mask_method is not None:
        print(f"precip. intensity threshold: {precip_thr}")

    num_ensemble_workers = n_ens_members if num_workers > n_ens_members else num_workers

    if measure_time:
        starttime_init = time.time()

    fft = utils.get_method(fft_method, shape=precip.shape[1:], n_threads=num_workers)

    M, N = precip.shape[1:]

    # initialize the band-pass filter
    filter_method = cascade.get_method(bandpass_filter_method)
    filter = filter_method((M, N), n_cascade_levels, **filter_kwargs)

    decomp_method, recomp_method = cascade.get_method(decomp_method)

    extrapolator_method = extrapolation.get_method(extrap_method)

    x_values, y_values = np.meshgrid(
        np.arange(precip.shape[2]), np.arange(precip.shape[1])
    )

    xy_coords = np.stack([x_values, y_values])

    precip = precip[-(ar_order + 1) :, :, :].copy()

    # determine the domain mask from non-finite values
    domain_mask = np.logical_or.reduce(
        [~np.isfinite(precip[i, :]) for i in range(precip.shape[0])]
    )

    # determine the precipitation threshold mask
    if conditional:
        mask_thr = np.logical_and.reduce(
            [precip[i, :, :] >= precip_thr for i in range(precip.shape[0])]
        )
    else:
        mask_thr = None

    # advect the previous precipitation fields to the same position with the
    # most recent one (i.e. transform them into the Lagrangian coordinates)
    extrap_kwargs = extrap_kwargs.copy()
    extrap_kwargs["xy_coords"] = xy_coords
    extrap_kwargs["allow_nonfinite_values"] = True
    res = list()

    def f(precip, i):
        return extrapolator_method(
            precip[i, :, :], velocity, ar_order - i, "min", **extrap_kwargs
        )[-1]

    for i in range(ar_order):
        if not DASK_IMPORTED:
            precip[i, :, :] = f(precip, i)
        else:
            res.append(dask.delayed(f)(precip, i))

    if DASK_IMPORTED:
        num_workers_ = len(res) if num_workers > len(res) else num_workers
        precip = np.stack(
            list(dask.compute(*res, num_workers=num_workers_)) + [precip[-1, :, :]]
        )

    # replace non-finite values with the minimum value
    precip = precip.copy()
    for i in range(precip.shape[0]):
        precip[i, ~np.isfinite(precip[i, :])] = np.nanmin(precip[i, :])

    if noise_method is not None:
        # get methods for perturbations
        init_noise, generate_noise = noise.get_method(noise_method)

        # initialize the perturbation generator for the precipitation field
        pp = init_noise(precip, fft_method=fft, **noise_kwargs)

        if noise_stddev_adj == "auto":
            print("Computing noise adjustment coefficients... ", end="", flush=True)
            if measure_time:
                starttime = time.time()

            precip_min = np.min(precip)
            noise_std_coeffs = noise.utils.compute_noise_stddev_adjs(
                precip[-1, :, :],
                precip_thr,
                precip_min,
                filter,
                decomp_method,
                pp,
                generate_noise,
                20,
                conditional=True,
                num_workers=num_workers,
            )

            if measure_time:
                print(f"{time.time() - starttime:.2f} seconds.")
            else:
                print("done.")
        elif noise_stddev_adj == "fixed":
            f = lambda k: 1.0 / (0.75 + 0.09 * k)
            noise_std_coeffs = [f(k) for k in range(1, n_cascade_levels + 1)]
        else:
            noise_std_coeffs = np.ones(n_cascade_levels)

        if noise_stddev_adj is not None:
            print(f"noise std. dev. coeffs:   {str(noise_std_coeffs)}")

    # compute the cascade decompositions of the input precipitation fields
    precip_d = []
    for i in range(ar_order + 1):
        precip_ = decomp_method(
            precip[i, :, :],
            filter,
            mask=mask_thr,
            fft_method=fft,
            output_domain=domain,
            normalize=True,
            compute_stats=True,
            compact_output=True,
        )
        precip_d.append(precip_)

    # normalize the cascades and rearrange them into a four-dimensional array
    # of shape (n_cascade_levels,ar_order+1,m,n) for the autoregressive model
    precip_c = nowcast_utils.stack_cascades(precip_d, n_cascade_levels)

    precip_d = precip_d[-1]
    precip_d = [precip_d.copy() for j in range(n_ens_members)]

    # compute lag-l temporal autocorrelation coefficients for each cascade level
    gamma = np.empty((n_cascade_levels, ar_order))
    for i in range(n_cascade_levels):
        gamma[i, :] = correlation.temporal_autocorrelation(precip_c[i], mask=mask_thr)

    nowcast_utils.print_corrcoefs(gamma)

    if ar_order == 2:
        # adjust the lag-2 correlation coefficient to ensure that the AR(p)
        # process is stationary
        for i in range(n_cascade_levels):
            gamma[i, 1] = autoregression.adjust_lag2_corrcoef2(gamma[i, 0], gamma[i, 1])

    # estimate the parameters of the AR(p) model from the autocorrelation
    # coefficients
    phi = np.empty((n_cascade_levels, ar_order + 1))
    for i in range(n_cascade_levels):
        phi[i, :] = autoregression.estimate_ar_params_yw(gamma[i, :])

    nowcast_utils.print_ar_params(phi)

    # discard all except the p-1 last cascades because they are not needed for
    # the AR(p) model
    precip_c = [precip_c[i][-ar_order:] for i in range(n_cascade_levels)]

    # stack the cascades into a list containing all ensemble members
    precip_c = [
        [precip_c[j].copy() for j in range(n_cascade_levels)]
        for i in range(n_ens_members)
    ]

    # initialize the random generators
    if noise_method is not None:
        randgen_prec = []
        randgen_motion = []
        np.random.seed(seed)
        for j in range(n_ens_members):
            rs = np.random.RandomState(seed)
            randgen_prec.append(rs)
            seed = rs.randint(0, high=1e9)
            rs = np.random.RandomState(seed)
            randgen_motion.append(rs)
            seed = rs.randint(0, high=1e9)

    if vel_pert_method is not None:
        init_vel_noise, generate_vel_noise = noise.get_method(vel_pert_method)

        # initialize the perturbation generators for the motion field
        vps = []
        for j in range(n_ens_members):
            kwargs = {
                "randstate": randgen_motion[j],
                "p_par": vp_par,
                "p_perp": vp_perp,
            }
            vp_ = init_vel_noise(velocity, 1.0 / kmperpixel, timestep, **kwargs)
            vps.append(vp_)

    displacement = [None for j in range(n_ens_members)]
    precip_f = [[] for j in range(n_ens_members)]

    if probmatching_method == "mean":
        mu_0 = np.mean(precip[-1, :, :][precip[-1, :, :] >= precip_thr])

    precip_m = None

    if mask_method is not None:
        mask_prec = precip[-1, :, :] >= precip_thr

        if mask_method == "obs":
            pass
        elif mask_method == "sprog":
            # compute the wet area ratio and the precipitation mask
            war = 1.0 * np.sum(mask_prec) / (precip.shape[1] * precip.shape[2])
            precip_m = [precip_c[0][i].copy() for i in range(n_cascade_levels)]
            precip_m_d = precip_d[0].copy()
        elif mask_method == "incremental":
            # get mask parameters
            mask_rim = mask_kwargs.get("mask_rim", 10)
            mask_f = mask_kwargs.get("mask_f", 1.0)
            # initialize the structuring element
            struct = scipy.ndimage.generate_binary_structure(2, 1)
            # iterate it to expand it nxn
            n = mask_f * timestep / kmperpixel
            struct = scipy.ndimage.iterate_structure(struct, int((n - 1) / 2.0))
            # initialize precip mask for each member
            mask_prec = _compute_incremental_mask(mask_prec, struct, mask_rim)
            mask_prec = [mask_prec.copy() for j in range(n_ens_members)]

    if noise_method is None and precip_m is None:
        precip_m = [precip_c[0][i].copy() for i in range(n_cascade_levels)]

    fft_objs = []
    for i in range(n_ens_members):
        fft_objs.append(utils.get_method(fft_method, shape=precip.shape[1:]))

    if measure_time:
        init_time = time.time() - starttime_init

    precip = precip[-1, :, :]

    print("Starting nowcast computation.")

    if measure_time:
        starttime_mainloop = time.time()

    if isinstance(timesteps, int):
        timesteps = range(timesteps + 1)
        timestep_type = "int"
    else:
        original_timesteps = [0] + list(timesteps)
        timesteps = nowcast_utils.binned_timesteps(original_timesteps)
        timestep_type = "list"

    extrap_kwargs["return_displacement"] = True
    precip_f_prev = [precip for i in range(n_ens_members)]
    t_prev = [0.0 for j in range(n_ens_members)]
    t_total = [0.0 for j in range(n_ens_members)]

    def decode_state(state, params):
        pass

    def update_state(state, params):
        pass

    # the nowcast iteration for each ensemble member
    def worker(j, t, subtimesteps):
        if noise_method is not None:
            # generate noise field
            eps = generate_noise(
                pp, randstate=randgen_prec[j], fft_method=fft_objs[j], domain=domain
            )

            # decompose the noise field into a cascade
            eps = decomp_method(
                eps,
                filter,
                fft_method=fft_objs[j],
                input_domain=domain,
                output_domain=domain,
                compute_stats=True,
                normalize=True,
                compact_output=True,
            )
        else:
            eps = None

        # iterate the AR(p) model for each cascade level
        for i in range(n_cascade_levels):
            # normalize the noise cascade
            if eps is not None:
                eps_ = eps["cascade_levels"][i]
                eps_ *= noise_std_coeffs[i]
            else:
                eps_ = None
            # apply AR(p) process to cascade level
            if eps is not None or vel_pert_method is not None:
                precip_c[j][i] = autoregression.iterate_ar_model(
                    precip_c[j][i], phi[i, :], eps=eps_
                )
            else:
                # use the deterministic AR(p) model computed above if
                # perturbations are disabled
                precip_c[j][i] = precip_m[i]

        eps = None
        eps_ = None

        # compute the recomposed precipitation field(s) from the cascades
        # obtained from the AR(p) model(s)
        precip_d[j]["cascade_levels"] = [
            precip_c[j][i][-1, :] for i in range(n_cascade_levels)
        ]
        if domain == "spatial":
            precip_d[j]["cascade_levels"] = np.stack(precip_d[j]["cascade_levels"])
        precip_f_new = recomp_method(precip_d[j])

        if domain == "spectral":
            precip_f_new = fft_objs[j].irfft2(precip_f_new)

        if mask_method is not None:
            # apply the precipitation mask to prevent generation of new
            # precipitation into areas where it was not originally
            # observed
            R_cmin = precip_f_new.min()
            if mask_method == "incremental":
                precip_f_new = R_cmin + (precip_f_new - R_cmin) * mask_prec[j]
                mask_prec_ = precip_f_new > R_cmin
            else:
                mask_prec_ = mask_prec

            # Set to min value outside of mask
            precip_f_new[~mask_prec_] = R_cmin

        if probmatching_method == "cdf":
            # adjust the CDF of the forecast to match the most recently
            # observed precipitation field
            precip_f_new = probmatching.nonparam_match_empirical_cdf(
                precip_f_new, precip
            )
        elif probmatching_method == "mean":
            MASK = precip_f_new >= precip_thr
            mu_fct = np.mean(precip_f_new[MASK])
            precip_f_new[MASK] = precip_f_new[MASK] - mu_fct + mu_0

        if mask_method == "incremental":
            mask_prec[j] = _compute_incremental_mask(
                precip_f_new >= precip_thr, struct, mask_rim
            )

        precip_f_new[domain_mask] = np.nan

        precip_f_out = []
        extrap_kwargs_ = extrap_kwargs.copy()

        vel_pert = velocity

        # advect the recomposed precipitation field to obtain the forecast for
        # the current time step (or subtimesteps if non-integer time steps are
        # given)
        for t_sub in subtimesteps:
            if t_sub > 0:
                t_diff_prev_int = t_sub - int(t_sub)
                if t_diff_prev_int > 0.0:
                    precip_f_ip = (1.0 - t_diff_prev_int) * precip_f_prev[
                        j
                    ] + t_diff_prev_int * precip_f_new
                else:
                    precip_f_ip = precip_f_prev[j]

                t_diff_prev = t_sub - t_prev[j]
                t_total[j] += t_diff_prev

                # compute the perturbed motion field
                if vel_pert_method is not None:
                    vel_pert = velocity + generate_vel_noise(
                        vps[j], t_total[j] * timestep
                    )

                extrap_kwargs_["displacement_prev"] = displacement[j]
                precip_f_ep, displacement[j] = extrapolator_method(
                    precip_f_ip,
                    vel_pert,
                    [t_diff_prev],
                    **extrap_kwargs_,
                )
                precip_f_out.append(precip_f_ep[0])
                t_prev[j] = t_sub

        # advect the forecast field by one time step if no subtimesteps in the
        # current interval were found
        if not subtimesteps:
            t_diff_prev = t + 1 - t_prev[j]
            t_total[j] += t_diff_prev

            # compute the perturbed motion field
            if vel_pert_method is not None:
                vel_pert = velocity + generate_vel_noise(vps[j], t_total[j] * timestep)

            extrap_kwargs_["displacement_prev"] = displacement[j]
            _, displacement[j] = extrapolator_method(
                None,
                vel_pert,
                [t_diff_prev],
                **extrap_kwargs_,
            )
            t_prev[j] = t + 1

        precip_f_prev[j] = precip_f_new

        return precip_f_out

    # iterate each time step
    for t, subtimestep_idx in enumerate(timesteps):
        if timestep_type == "list":
            subtimesteps = [original_timesteps[t_] for t_ in subtimestep_idx]
        else:
            subtimesteps = [t]

        if (timestep_type == "list" and subtimesteps) or (
            timestep_type == "int" and t > 0
        ):
            is_nowcast_time_step = True
        else:
            is_nowcast_time_step = False

        if is_nowcast_time_step:
            print(
                f"Computing nowcast for time step {t}... ",
                end="",
                flush=True,
            )

        if measure_time:
            starttime = time.time()

        if noise_method is None or mask_method == "sprog":
            for i in range(n_cascade_levels):
                # use a separate AR(p) model for the non-perturbed forecast,
                # from which the mask is obtained
                precip_m[i] = autoregression.iterate_ar_model(precip_m[i], phi[i, :])

            precip_m_d["cascade_levels"] = [
                precip_m[i][-1] for i in range(n_cascade_levels)
            ]
            if domain == "spatial":
                precip_m_d["cascade_levels"] = np.stack(precip_m_d["cascade_levels"])
            precip_m_ = recomp_method(precip_m_d)
            if domain == "spectral":
                precip_m_ = fft.irfft2(precip_m_)

            if mask_method == "sprog":
                mask_prec = compute_percentile_mask(precip_m_, war)

        res = []
        for j in range(n_ens_members):
            if not DASK_IMPORTED or n_ens_members == 1:
                res.append(worker(j, t, subtimesteps))
            else:
                res.append(dask.delayed(worker)(j, t, subtimesteps))

        precip_f_ = (
            dask.compute(*res, num_workers=num_ensemble_workers)
            if DASK_IMPORTED and n_ens_members > 1
            else res
        )
        res = None

        if is_nowcast_time_step:
            if measure_time:
                print(f"{time.time() - starttime:.2f} seconds.")
            else:
                print("done.")

        if callback is not None:
            precip_f_stacked = np.stack(precip_f_)
            if precip_f_stacked.shape[1] > 0:
                callback(precip_f_stacked.squeeze())

        if return_output:
            for j in range(n_ens_members):
                precip_f[j].extend(precip_f_[j])

        precip_f_ = None

    if measure_time:
        mainloop_time = time.time() - starttime_mainloop

    if return_output:
        outarr = np.stack([np.stack(precip_f[j]) for j in range(n_ens_members)])
        if measure_time:
            return outarr, init_time, mainloop_time
        else:
            return outarr
    else:
        return None


def _check_inputs(precip, velocity, timesteps, ar_order):
    if precip.ndim != 3:
        raise ValueError("precip must be a three-dimensional array")
    if precip.shape[0] < ar_order + 1:
        raise ValueError("precip.shape[0] < ar_order+1")
    if velocity.ndim != 3:
        raise ValueError("velocity must be a three-dimensional array")
    if precip.shape[1:3] != velocity.shape[1:3]:
        raise ValueError(
            "dimension mismatch between precip and velocity: shape(precip)=%s, shape(velocity)=%s"
            % (str(precip.shape), str(velocity.shape))
        )
    if isinstance(timesteps, list) and not sorted(timesteps) == timesteps:
        raise ValueError("timesteps is not in ascending order")


def _compute_incremental_mask(Rbin, kr, r):
    # buffer the observation mask Rbin using the kernel kr
    # add a grayscale rim r (for smooth rain/no-rain transition)

    # buffer observation mask
    Rbin = np.ndarray.astype(Rbin.copy(), "uint8")
    Rd = scipy.ndimage.morphology.binary_dilation(Rbin, kr)

    # add grayscale rim
    kr1 = scipy.ndimage.generate_binary_structure(2, 1)
    mask = Rd.astype(float)
    for n in range(r):
        Rd = scipy.ndimage.morphology.binary_dilation(Rd, kr1)
        mask += Rd
    # normalize between 0 and 1
    return mask / mask.max()
