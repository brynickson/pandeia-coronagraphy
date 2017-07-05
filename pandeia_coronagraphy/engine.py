from copy import deepcopy
import json
import multiprocessing as mp
from functools32 import lru_cache

import pandeia
from pandeia.engine.instrument_factory import InstrumentFactory
from pandeia.engine.psf_library import PSFLibrary
from pandeia.engine.perform_calculation import perform_calculation as pandeia_calculation
from pandeia.engine.astro_spectrum import * 

try:
    import webbpsf
except ImportError:
    pass

from . import transformations

# Pandeia defaults to ~200 wavelength samples for imaging
# This is slow and unlikely to significantly improve the
# accuracy of coronagraphic performance predictions.
# Setting wave_sampling to 10-20 should be sufficient,
# and translates to a time savings of about 10.
# Leaving it at None sets it to Pandeia's default.
wave_sampling = None
# Avoid Pandeia's precomputed PSFs and recompute in WebbPSF as needed?
on_the_fly_PSFs = False
pandeia_get_psf = PSFLibrary.get_psf

on_the_fly_webbpsf_options = dict() # Extra options for configuring the PSF calculation ad hoc.
                                    # note some options are overridden in get_psf_on_the_fly()
on_the_fly_webbpsf_opd = None       # Allow overriding the default OPD selection when computing PSFs on the fly
on_the_fly_cache_maxsize = 256      # Number of monochromatic PSFs stored in an LRU cache
                                    # Should speed up calculations that involve modifying things
                                    # like exposure time and don't actually require calculating new PSFs. 

latest_on_the_fly_PSF = None

def get_template(filename):
    """ Look up a template filename. Assumes template files are stored in a fixed location relative
    to this pandeia_coronagraphy source code. Still pretty brittle but better than hard-coding
    paths in the notebooks.
    """
    import os
    thisdir = os.path.dirname(os.path.abspath(__file__))
    outfile = os.path.join(thisdir,"../templates", filename)
    outfile = os.path.abspath(outfile)
    return outfile


def load_calculation(filename):
    with open(filename) as f:
        calcfile = json.load(f)
    return calcfile

def save_calculation(calcfile,filename):
    with open(filename,'w+') as f:
        json.dump(calcfile,f,indent=2)

def save_to_fits(array,filename):
    hdu = fits.PrimaryHDU(array)
    hdulist = fits.HDUList([hdu])
    hdulist.writeto(filename)

def calculate_batch(calcfiles,nprocesses=None):
    if nprocesses is None:
        nprocesses = mp.cpu_count()
    pool = mp.Pool(processes=nprocesses)
    results = pool.map(perform_calculation,calcfiles)
    pool.close()
    pool.join()
    return results

def perform_calculation(calcfile):
    '''
    Manually decorate pandeia.engine.perform_calculation to circumvent
    pandeia's tendency to modify the calcfile during the calculation.

    Updates to the saturation computation could go here as well.
    '''
    if on_the_fly_PSFs:
        pandeia.engine.psf_library.PSFLibrary.get_psf = on_the_fly_psf_wrapper
    else:
        pandeia.engine.psf_library.PSFLibrary.get_psf = pandeia_get_psf

    calcfile = deepcopy(calcfile)
    results = pandeia_calculation(calcfile)

    if on_the_fly_PSFs:
        # Append PSF that was computed on-the-fly
        results['psf'] = latest_on_the_fly_PSF

    #get fullwell for instrument + detector combo
    #instrument = InstrumentFactory(config=calcfile['configuration'])
    #fullwell = instrument.get_detector_pars()['fullwell']

    #recompute saturated pixels and populate saturation and detector images appropriately
    #image = results['2d']['detector'] * results['information']['exposure_specification']['ramp_exposure_time']
    #saturation = np.zeros_like(image)
    #saturation[image > fullwell] = 1
    #results['2d']['saturation'] = saturation
    #results['2d']['detector'][saturation.astype(bool)] = np.nan

    return results

def on_the_fly_psf_wrapper(self,*args,**kwargs):
    '''
    An additional layer to allow the use of lru_cache on
    on-the-fly PSFs (which requires hashable inputs)

    '''
    global latest_on_the_fly_PSF


    # Include the on-the-fly override options in the hash key for the lru_cache
    otf_options = tuple(sorted(on_the_fly_webbpsf_options.items()) +
            [on_the_fly_webbpsf_opd,])
    tmp = get_psf_on_the_fly(*args,**kwargs,otf_options=otf_options)
    latest_on_the_fly_PSF = deepcopy(tmp)
    return tmp

@lru_cache(maxsize=on_the_fly_cache_maxsize)
def get_psf_on_the_fly(wave, instrument, aperture_name, source_offset=(0, 0), otf_options=None):
    #Make the instrument and determine the mode
    if instrument.upper() == 'NIRCAM':
        ins = webbpsf.NIRCam()

        if wave > 2.5:
            # need to toggle to LW detector.
            ins.detector='A5'
            ins.pixelscale = ins._pixelscale_long
    else:
        ins = webbpsf.Instrument(instrument)
    image_mask, pupil_mask, fov_pixels, trim_fov_pixels, pix_scl = parse_aperture(aperture_name, instrument.upper())
    ins.image_mask = image_mask
    ins.pupil_mask = pupil_mask

    # Apply any extra options if specified by the user:
    for key in on_the_fly_webbpsf_options:
        ins.options[key] = on_the_fly_webbpsf_options[key]

    # Note, here a Python None object means 'use default OPD', but
    # specially check for the string 'none' in which case make it
    # use NO OPD.
    if on_the_fly_webbpsf_opd is not None:
        ins.pupilopd = on_the_fly_webbpsf_opd
        if on_the_fly_webbpsf_opd =='none':
            ins.pupilopd = None

    #get offset
    ins.options['source_offset_r'] = source_offset[0]
    ins.options['source_offset_theta'] = source_offset[1]
    ins.options['output_mode'] = 'oversampled'
    ins.options['parity'] = 'odd'
    ins.pixelscale = pix_scl   # ensure precise agreement w/ what Pandeia expects

    psf_result = calc_psf_and_center(ins, wave, source_offset[0], source_offset[1], 3,
                            pix_scl, fov_pixels, trim_fov_pixels=trim_fov_pixels)

    pix_scl = psf_result[0].header['PIXELSCL']
    upsamp = psf_result[0].header['OVERSAMP']
    diff_limit = psf_result[0].header['DIFFLMT']
    psf = psf_result[0].data

    psf = {
        'int': psf,
        'wave': wave,
        'pix_scl': pix_scl,
        'diff_limit': diff_limit,
        'upsamp': upsamp,
        'instrument': instrument,
        'aperture_name': aperture_name,
        'source_offset': source_offset
    }

    return psf

def parse_aperture(aperture_name, instrument_name):
    '''
    Return [image mask, pupil mask, fov_pixels, trim_fov_pixels, pixelscale]

    '''


    if instrument_name =='NIRCAM':
        nc = webbpsf.NIRCam()
        aperture_dict = {
            'mask210r' : ['MASK210R','CIRCLYOT', 101, None, nc._pixelscale_short],
            'mask335r' : ['MASK335R','CIRCLYOT', 101, None, nc._pixelscale_long],
            'mask430r' : ['MASK430R','CIRCLYOT', 101, None, nc._pixelscale_long],
            'masklwb' : ['MASKLWB','WEDGELYOT', 351, 101, nc._pixelscale_long],
            'maskswb' : ['MASKSWB','WEDGELYOT', 351, 101, nc._pixelscale_short],
            'sw': [None, None, 101, None, nc._pixelscale_short],
            'lw': [None, None, 101, None, nc._pixelscale_short],
            }
    elif instrument_name=='MIRI':
        miri = webbpsf.MIRI()
        aperture_dict = {
            'fqpm1065' : ['FQPM1065','MASKFQPM', 81, None, miri.pixelscale],
            'fqpm1140' : ['FQPM1140','MASKFQPM', 81, None, miri.pixelscale],
            'fqpm1550' : ['FQPM1550','MASKFQPM', 81, None, miri.pixelscale],
            'lyot2300' : ['LYOT2300','MASKLYOT', 81, None, miri.pixelscale],
            'imager' : [None,None, 81, None, 0.11 ], # slightly diff from the 0.111 in webbpsf, but has to match Pandeia
                                                     # exactly or we trigger an error in engine.astro_spectrum.AdvancedPSF
                                                     # I dunno why this is the case for MIRI imaging but not coronagraphy. ??
            }
    elif instrument_name =='NIRISS' or instrument_name=='NIRSPEC' or instrument_name=='FGS':
        desired_psf_size ={'NIRISS':81, 'NIRSPEC':41, "FGS":81}
        inst = webbpsf.Instrument(instrument_name)
        aperture_dict = {
            'imager': [None, None, desired_psf_size[instrument_name], None, inst.pixelscale],
            }
    else:
        raise NotImplementedError("Instrument {} is not yet supported for on-the-fly PSFs; need to update engine.py".format(instrument_name))


    aperture_keys = aperture_dict.keys()

    try:
        # MP: I don't understand what this next line is doing - seems an unnecessarily
        # complicated assignment which is often in practice an identity? 
        aperture = [a for a in aperture_keys if a in aperture_name][0]
    except:
        raise ValueError("Aperture {} is not yet supported for on-the-fly PSFs; need to update engine.py".format(aperture_name))

    return aperture_dict[aperture]

def calc_psf_and_center(ins, wave, offset_r, offset_theta, oversample, pix_scale, fov_pixels, trim_fov_pixels=None):
    '''
    Following the treatment in pandeia_data/dev/make_psf.py to handle
    off-center PSFs for use as a kernel in later convolutions.
    '''

    if offset_r > 0.:
        #roll back to center
        dx = int(np.rint( offset_r * np.sin(np.deg2rad(offset_theta)) / pix_scale ))
        dy = int(np.rint( offset_r * np.cos(np.deg2rad(offset_theta)) / pix_scale ))
        dmax = np.max([np.abs(dx), np.abs(dy)])

        # pandeia forces offset to nearest integer subsampled pixel.
        # At the risk of having subpixel offsets in the recentering,
        # I'm not sure we want to do this in order to capture
        # small-scale spatial variations properly.
        #ins.options['source_offset_r'] = np.sqrt(dx**2 + dy**2) * pix_scale

        psf_result = ins.calc_psf(monochromatic=wave*1e-6, oversample=oversample, fov_pixels=fov_pixels + 2*dmax)

        image = psf_result[0].data
        image = np.roll(image, dx * oversample, axis=1)
        image = np.roll(image, -dy * oversample, axis=0)
        image = image[dmax * oversample:(fov_pixels + dmax) * oversample,
                      dmax * oversample:(fov_pixels + dmax) * oversample]
        #trim if requested
        if trim_fov_pixels is not None:
            trim_amount = int(oversample * (fov_pixels - trim_fov_pixels) / 2)
            image = image[trim_amount:-trim_amount, trim_amount:-trim_amount]
        psf_result[0].data = image
    else:
        psf_result = ins.calc_psf(monochromatic=wave*1e-6, oversample=oversample, fov_pixels=fov_pixels)

    return psf_result

def ConvolvedSceneCubeinit(self, scene, instrument, background=None, psf_library=None, webapp=False):
    '''
    An almost exact copy of pandeia.engine.astro_spectrum.ConvolvedSceneCube.__init__,
    unfortunately reproduced here to circumvent the wave sampling behavior.

    See the nw_maximal variable toward the end of this function. It's now controlled
    by the global wave_sampling defined in this module.
    '''
    self.warnings = {}
    self.scene = scene
    self.psf_library = psf_library
    self.aper_width = instrument.get_aperture_pars()['disp']
    self.aper_height = instrument.get_aperture_pars()['xdisp']
    self.multishutter = instrument.get_aperture_pars()['multishutter']
    nslice_str = instrument.get_aperture_pars()['nslice']

    if nslice_str is not None:
        self.nslice = int(nslice_str)
    else:
        self.nslice = 1

    self.instrument = instrument
    self.background = background

    self.fov_size = self.get_fov_size()

    # Figure out what the relevant wavelength range is, given the instrument mode
    wrange = self.current_instrument.get_wave_range()

    self.source_spectra = []

    # run through the sources and check their wavelength extents. warn if they fall short of the
    # current instrument configuration's range.
    mins = []
    maxes = []
    key = None
    for i, src in enumerate(scene.sources):
        spectrum = AstroSpectrum(src, webapp=webapp)
        self.warnings.update(spectrum.warnings)
        smin = spectrum.wave.min()
        smax = spectrum.wave.max()
        if smin > wrange['wmin']:
            if smin > wrange['wmax']:
                key = "spectrum_missing_red"
                msg = warning_messages[key] % (smin, smax, wrange['wmax'])
                self.warnings["%s_%s" % (key, i)] = msg
            else:
                key = "wavelength_truncated_blue"
                msg = warning_messages[key] % (smin, wrange['wmin'])
                self.warnings["%s_%s" % (key, i)] = msg
        if smax < wrange['wmax']:
            if smax < wrange['wmin']:
                key = "spectrum_missing_blue"
                msg = warning_messages[key] % (smin, smax, wrange['wmin'])
                self.warnings["%s_%s" % (key, i)] = msg
            else:
                key = "wavelength_truncated_red"
                msg = warning_messages[key] % (smax, wrange['wmax'])
                self.warnings["%s_%s" % (key, i)] = msg

        mins.append(smin)
        maxes.append(smax)

    wmin = max([np.array(mins).min(), wrange['wmin']])
    wmax = min([np.array(maxes).max(), wrange['wmax']])

    # make sure we have something within range and error out otherwise
    if wmax < wrange['wmin'] or wmin > wrange['wmax']:
        msg = "No wavelength overlap between source_spectra [%.2f, %.2f] and instrument [%.2f, %.2f]." % (
            np.array(mins).min(),
            np.array(maxes).max(),
            wrange['wmin'],
            wrange['wmax']
        )
        raise RangeError(value=msg)

    # warn if partial overlap between combined wavelength range of all sources and the instrument's wrange
    if wmin != wrange['wmin'] or wmax != wrange['wmax']:
        key = "scene_range_truncated"
        self.warnings[key] = warning_messages[key] % (wmin, wmax, wrange['wmin'], wrange['wmax'])

    """
    Trim spectrum and do the spectral convolution here on a per-spectrum basis.  Most efficient to do it here
    before the wavelength sets are merged.  Also easier and much more efficient than convolving
    an axis of a 3D cube.
    """
    for src in scene.sources:
        spectrum = AstroSpectrum(src, webapp=webapp)
        # we trim here as an optimization so that we only convolve the section we need of a possibly very large spectrum
        spectrum.trim(wrange['wmin'], wrange['wmax'])
        spectrum = instrument.spectrometer_convolve(spectrum)
        self.source_spectra.append(spectrum)

    """
    different spectra will have different sets of wavelengths. the obvious future
    case will be user-supplied spectra, but this is also true for analytic spectra
    that have different emission/absorption lines. go through each of the spectra,
    merge all of the wavelengths sets into one, and then resample each spectrum
    onto the combined wavelength set.
    """
    self.wave = self.source_spectra[0].wave
    for s in self.source_spectra:
        self.wave = merge_wavelengths(self.wave, s.wave)

    projection_type = instrument.projection_type

    """
    For the spectral projections, we could use the pixel sampling. However, this
    may oversample the cube for input spectra with no narrow features. So we first check
    whether the pixel sampling will give us a speed advantage. Otherwise, do not resample to
    an unnecessarily fine wavelength grid.
    """
    if projection_type in ('spec', 'slitless', 'multiorder'):
        wave_pix = instrument.get_wave_pix()
        if wave_pix.size < self.wave.size:
            self.wave = wave_pix[np.where(np.logical_and(wave_pix >= wrange['wmin'],
                                                         wave_pix <= wrange['wmax']))]

    """
    There is no inherently optimal sampling for imaging modes, but we resample here to
    a reasonable number of wavelength bins if necessary. This helps keep the cube rendering reasonable
    for large input spectra. Note that the spectrum resampling now uses the flux conserving method
    of pysynphot.
    """
    if projection_type == 'image':
        """
        In practice a value of 200 samples within an imaging configuration's wavelength range
        (i.e. filter bandpass) should be more than enough. Note that because we use pysynphot
        to resample, the flux of even narrow lines is conserved.
        """
        if wave_sampling is None:
            nw_maximal = 200 #pandeia default
        else:
            nw_maximal = wave_sampling
        if self.wave.size > nw_maximal:
            self.wave = np.linspace(wrange['wmin'], wrange['wmax'], nw_maximal)

    self.nw = self.wave.size
    self.total_flux = np.zeros(self.nw)

    for spectrum in self.source_spectra:
        spectrum.resample(self.wave)
        self.total_flux += spectrum.flux

    # also need to resample the background spectrum
    if self.background is not None:
        self.background.resample(self.wave)

    self.grid, self.aperture_list, self.flux_cube_list, self.flux_plus_bg_list = \
        self.create_flux_cube(background=self.background)
    self.dist = self.grid.dist()
    
pandeia.engine.astro_spectrum.ConvolvedSceneCube.__init__ = ConvolvedSceneCubeinit


def _make_dither_weights(self):
    '''
    Hack to circumvent reference subtraction in pandeia,
    which is currently incorrect, as well as turn off
    the additional calculations for the contrast calculation.
    This gives us about a factor of 3 in time savings.
    '''
    self.dither_weights = [1,0,0] #Pandeia: [1,-1,0]
    del self.calc_type
    
pandeia.engine.strategy.Coronagraphy._make_dither_weights = _make_dither_weights
pandeia.engine.strategy.Coronagraphy._create_weight_matrix = pandeia.engine.strategy.ImagingApPhot._create_weight_matrix