#!/usr/bin/env python

import os, sys
import argparse
import configparser as ConfigParser
import ast

import logging
import sys
import time

import numpy as np
import pylab as py

from rascil.data_models import PolarisationFrame
from rascil.processing_components import (create_blockvisibility_from_ms,
                                          create_image_from_visibility,
                                          show_image,
                                          qa_image,
                                          deconvolve_cube,
                                          restore_cube,
                                          export_image_to_fits,
                                          advise_wide_field,
                                          create_calibration_controls,
                                          convert_blockvisibility_to_stokesI)
from rascil.workflows import invert_list_rsexecute_workflow, weight_list_rsexecute_workflow, \
    ical_list_rsexecute_workflow, sum_invert_results_rsexecute
from rascil.workflows.rsexecute.execution_support.rsexecute import rsexecute, get_dask_client

log = logging.getLogger("logger")
log.setLevel(logging.INFO)
log.addHandler(logging.StreamHandler(sys.stdout))

# --------------------------------------------------------------------------------------------------

def parse_args():
    """
        Parse the command line arguments
        """
    parser = argparse.ArgumentParser()
    parser.add_argument('-C','--config', default="myconfig.txt", required=False, help='Name of the input config file')
    parser.add_argument('-T','--threads', default=2, required=False, help='Number of threads [default:2]')
    
    args, __ = parser.parse_known_args()
    
    return vars(args)

# --------------------------------------------------------------------------------------------------

def parse_config(filename):
    """
        Given an input config file, parses it to extract key-value pairs that
        should represent task parameters and values respectively.
        """
    
    config = ConfigParser.SafeConfigParser(allow_no_value=True)
    config.read(filename)
    
    # Build a nested dictionary with tasknames at the top level
    # and parameter values one level down.
    taskvals = dict()
    for section in config.sections():
        
        if section not in taskvals:
            taskvals[section] = dict()
        
        for option in config.options(section):
            # Evaluate to the right type()
            try:
                taskvals[section][option] = ast.literal_eval(config.get(section, option))
            except (ValueError,SyntaxError):
                err = "Cannot format field '{0}' in config file '{1}'".format(option,filename)
                err += ", which is currently set to {0}. Ensure strings are in 'quotes'.".format(config.get(section, option))
                raise ValueError(err)

    return taskvals, config

# --------------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------

if __name__ == "__main__":
    tn = time.time()

    vars = parse_args()
    config_dict, config = parse_config(vars['config'])
    
    print(int(vars['threads']))
    print(int(config_dict['dask']['workers']))
    
    if int(vars['threads'])<int(config_dict['dask']['workers']):
        print("Number of dask workers larger than available processors. Resetting to ",int(vars['threads']))
        config_dict['dask']['threads'] = int(vars['threads'])
    
    # if os.path.isdir('./imaging_output') == True:
    #     shutil.rmtree('./imaging_output')
    # else:
    #     pass

    # List ms fields and spectral windows
    # list_MS = list_ms(parameters['mspath'], ack=True)
    ts = time.time()

    # Tell Dask to use 8 workers
    client = get_dask_client(n_workers=4)
    rsexecute.set_client(client)

    nchan = config_dict['imaging']['nchan']
    step_chan = config_dict['imaging']['step_chan']
    ndds = config_dict['imaging']['ndds']
    
    for source in config_dict['data']['sources']:

        # We load the MS's in chunks of 1920/8 =240 channels, iterating over all data descriptors. This gives
        # a list of BlockVis.

        print ('nchan: ',nchan,'    step_chan: ',step_chan)
        bvis_list = [[rsexecute.execute(create_blockvisibility_from_ms)(msname=config_dict['data']['mspath'],
                                                                        ack=False,
                                                                        datacolumn=config_dict['data']['data_column'],
                                                                        selected_sources=source,
                                                                        selected_dds=[dd],
                                                                        start_chan=chan, end_chan=chan + step_chan - 1,
                                                                        average_channels=30)[0]
                      for chan in range(0, nchan, step_chan)] for dd in range(ndds)]
                      
        # The result is a list of lists but we want a flattened list. The following does this.
        bvis_list = [item for sublist in bvis_list for item in sublist]
        
        # RASCIL 0.1.9b0 cannot calibrate linearnp data so we convert to stokesI
        bvis_list = [rsexecute.execute(convert_blockvisibility_to_stokesI)(bvis) for bvis in bvis_list]

        # Now compute the values of the list.
        bvis_list = rsexecute.compute(bvis_list, sync=True)

        # Use the advise function to tell us the cellsize
        vt = bvis_list[-1]
        print(vt)
        
        advice = advise_wide_field(vt, oversampling_synthesised_beam=4.0)
        baseline_max = advice['maximum_baseline']
        print('the maximum baseline of the configuration is; ', baseline_max)
        
        nchan = len(vt.frequency)
        
        # vt.channel_bandwidth = np.array(nchan * [(vt.frequency[1] - vt.frequency[0])])
        
        # check this vvvv cellsize seems to be in radians
        if advice['cellsize']<config_dict['imaging']['cellsize']: print("cellsize parameter may be too large, recommended value: ",advice['cellsize'])
        
        model_list = [create_image_from_visibility(bvis, cellsize=advice['cellsize'], npixel=config_dict['imaging']['npix'], nchan=1,
                                                   polarisation_frame=PolarisationFrame(config_dict['imaging']['stokes']))
                      for bvis in bvis_list]

        # The autocorrelations seem to not have been flagged
        for bvis in bvis_list:
            nants = bvis.vis.shape[1]
            for a in range(nants):
                bvis.weight[:, a, a, ...] = 0.0
                bvis.imaging_weight[:, a, a, ...] = 0.0

        # Send the model images and BlockVis to the Dask workers
        model_list = rsexecute.scatter(model_list)
        bvis_list = rsexecute.scatter(bvis_list)

        # Now weight the data
        bvis_list = weight_list_rsexecute_workflow(bvis_list, model_list, weighting=config_dict['imaging']['weighting'], robustness=config_dict['imaging']['robust'])

        do_selfcal = config_dict['selfcal']['do_selfcal']
        if do_selfcal:
            # Solve for phase only, starting after 1 major cycle, and then solve for complex gain on
            # 600s time scale after 3 iterations
            controls = create_calibration_controls()
            controls['T']['first_selfcal'] = 0
            controls['T']['phase_only'] = True
            controls['T']['timeslice'] = 'auto'
            controls['G']['first_selfcal'] = 2
            controls['G']['phase_only'] = False
            controls['G']['timeslice'] = 600.0

            # Set up the ICAL workflow which does an imaging/selfcal loop
            result = ical_list_rsexecute_workflow(bvis_list, model_list,
                                                  calibration_context=config_dict['selfcal']['calibration_context'],
                                                  context=config_dict['selfcal']['imaging_context'],
                                                  nmajor=config_dict['selfcal']['nloops'],
                                                  niter=config_dict['selfcal']['niter'],
                                                  algorithm=config_dict['selfcal']['algorithm'],
                                                  scales=[0],
                                                  nmoment=config_dict['selfcal']['moments'],
                                                  fractional_threshold=0.3,
                                                  threshold=config_dict['selfcal']['threshold'],
                                                  window_shape='quarter',
                                                  do_selfcal=do_selfcal,
                                                  global_solution=False,
                                                  controls=controls)

            comp, residual, restored, gt_list = rsexecute.compute(result, sync=True)
            export_image_to_fits(restored[0], "./imaging_output/{0}.fits".format('ical_restored_image_for_' + source))

            show_image(restored[0], vmin=0.8 * np.min(comp[0].data),
                       vmax=0.5 * np.max(comp[0].data),
                       title='restored image for ' + source)
            plotfile_restored = "./imaging_output/{0}.png".format('ical_restored_image_for_' + source)
            py.savefig(plotfile_restored)

            qa = qa_image(restored[0], context='ICAL restored image')
            log.info(qa)

        else:

            # Make the dirty images
            result = invert_list_rsexecute_workflow(vis_list=bvis_list,
                                                    template_model_imagelist=model_list,
                                                    context=config_dict['imaging']['imaging_context'],
                                                    dopsf=False)
                                                    
            # Sum over all dirty images
            result = sum_invert_results_rsexecute(result)
            
            # Finally we compute the graph and get the dirty image and sum of weights
            dirty, sumwt = rsexecute.compute(result, sync=True)

            result = invert_list_rsexecute_workflow(vis_list=bvis_list, template_model_imagelist=model_list,
                                                    context=config_dict['imaging']['imaging_context'],
                                                    dopsf=True)
                                                    
            result = sum_invert_results_rsexecute(result)
            psf, sumwt = rsexecute.compute(result, sync=True)
            export_image_to_fits(dirty, "./imaging_output/{0}.fits".format('dirty_image_for_' + source))
            export_image_to_fits(psf, "./imaging_output/{0}.fits".format('psf_image_for_' + source))

            # Now do the deconvolution. This is comparatively cheap so we don't use Dask
            comp, residual = deconvolve_cube(dirty, psf, niter=config_dict['imaging']['niter'], threshold=config_dict['imaging']['threshold'],
                                             gain=0.1, algorithm=config_dict['imaging']['algorithm'],
                                             window_shape='quarter')
                                             
            restored = restore_cube(comp, psf, residual)
            export_image_to_fits(psf, "./imaging_output/{0}.fits".format('psf_image_for_' + source))
            export_image_to_fits(restored, "./imaging_output/{0}.fits".format('restored_image_for_' + source))

            show_image(psf, title='psf image for ' + source)
            plotfile_psf = "./imaging_output/{0}.png".format('psf_image_for_' + source)
            py.savefig(plotfile_psf)

            show_image(dirty, title='diry image for ' + source)
            plotfile_dirty = "./imaging_output/{0}.png".format('dirty_image_for_' + source)
            py.savefig(plotfile_dirty)

            show_image(restored, vmin=0.8 * np.min(comp.data),
                       vmax=0.5 * np.max(comp.data),
                       title='restored image for ' + source)
            plotfile_restored = "./imaging_output/{0}.png".format('restored_image_for_' + source)
            py.savefig(plotfile_restored)

        ################Plot uv coverage ############
        fig = py.figure()
        py.plot(vt.data['uvw'][..., 0].flatten() / 1.0e6, vt.data['uvw'][..., 1].flatten() / 1.0e6, '.', markersize=0.5)
        py.plot(-vt.data['uvw'][..., 0].flatten() / 1.0e6, -vt.data['uvw'][..., 1].flatten() / 1.0e6, '.', markersize=0.5)
        py.xlabel(r'u (M$\lambda$)', fontweight='bold', fontsize=15)
        py.ylabel(r'v (M$\lambda$)', fontweight='bold', fontsize=15)
        py.xticks(fontweight='bold', fontsize=12)
        py.yticks(fontweight='bold', fontsize=12)
        py.title('UV Coverage' + ' for ' + source)
        py.tight_layout()
        fig.savefig('./imaging_output/uvcoverge_for_' + source + '.png', dpi=fig.dpi)

        #############Plot uvdist ############
        uvdist = np.sqrt(vt.data['uvw'][..., 0] ** 2 + vt.data['uvw'][..., 1] ** 2).flatten()
        fig2 = py.figure()
        py.plot(uvdist / 1.0e6, np.abs(vt.data['vis'])[..., 0].flatten(), 'k.', ms=0.4)
        py.xlabel(r'uv-dist (M$\lambda$)', fontweight='bold', fontsize=15)
        py.ylabel('Amp', fontweight='bold', fontsize=15)
        py.xticks(fontweight='bold', fontsize=12)
        py.yticks(fontweight='bold', fontsize=12)
        py.title('Visibility Amplitude plot ' + 'for ' + source)
        py.tight_layout()
        fig2.savefig('./imaging_output/amp_vs_uvdist_plot_for' + source + '.png', dpi=fig2.dpi)
