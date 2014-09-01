import numpy as np
import scipy.sparse
import scipy.sparse.linalg
import os
import sys
import emcee
from Model_objs import *
from GenerateLensingGrid import GenerateLensingGrid
from calc_likelihood import calc_vis_lnlike
import astropy.cosmology as ac
ac.set_current(ac.FlatLambdaCDM(H0=71.,Om0=0.2669))
#ac.set_current(ac.WMAP9)
cosmo = ac.get_current()
arcsec2rad = np.pi/180/3600

def LensModelMCMC(data,lens,source,shear=None,
                  xmax=30.,highresbox=[-3.,3.,3.,-3.],emitres=None,fieldres=None,
                  sourcedatamap=None, scaleamp=False, shiftphase=False,
                  modelcal=True,nwalkers=1e3,nburn=1e3,nstep=1e3,nthreads=1,mpirun=False):
      """
      Wrapper function which basically takes what the user wants and turns it into the
      format needed for the acutal MCMC lens modeling.
      
      Inputs:
      data:
            One or more visdata objects; if multiple datasets are being
            fit to, should be a list of visdata objects.
      lens:
            Any of the currently implemented lens objects.
      source:
            One or more of the currently implemented source objects; if more than
            one source to be fit, should be a list of multiple sources.
      shear:
            An ExternalShear object, or None (default) if no external shear desired
      xmax:
            (Half-)Grid size, in arcseconds; the grid will span +/-xmax in x&y
      highresbox:
            The region to model at higher resolution (to account for high-magnification
            and differential lensing effects). Note the sign convention is:
            +x = West, +y = South, so highresbox[2] will be > highresbox[3]
      sourcedatamap:
            A list of length the number of datasets which tells which source(s)
            are to be fit to which dataset(s). Eg, if two sources are to be fit
            to two datasets jointly, should be [[0,1],[0,1]]. If we have four
            sources and three datasets, could be [[0,1],[0,1],[2,3]] to say that the
            first two sources should both be fit to the first two datasets, while the
            second two should be fit to the third dataset. If None, will assume
            all sources should be fit to all datasets.
      scaleamp:
            A list of length the number of datasets which tells whether a flux
            rescaling is allowed and which dataset the scaling should be relative to.
            False indicates no scaling should be done, while True indicates that
            amplitude scaling should be allowed.
      shiftphase:
            Similar to scaleamp above, but allowing for positional/astrometric offsets.
      modelcal:
            Whether or not to perform the pseudo-selfcal procedure of H+13
      nwalkers:
            Number of walkers to use in the mcmc process; see dan.iel.fm/emcee/current
            for more details.
      nburn:
            Number of burn-in steps to take with the chain.
      nstep:
            Number of actual steps to take in the mcmc chains after the burn-in
      nthreads:
            Number of threads (read: cores) to use during the fitting, default 1.
      mpirun:
            Whether to parallelize using MPI instead of multiprocessing. If True,
            nthreads has no effect, and your script should be run with, eg,
            mpirun -np 16 python lensmodel.py.

      Returns:
      mcmcresult:
            A nested dict containing the chains requested. Will have all the MCMC
            chain results, plus metadata about the run (initial params, data used,
            etc.). Formatting still a work in progress (esp. for modelcal phases).
      chains:
            The raw chain data, for testing.
      blobs:
            Everything else returned by the likelihood function; will have
            magnifications and any modelcal phase offsets at each step; eventually
            will remove this once get everything packaged up for mcmcresult nicely.
      colnames:
            Basically all the keys to the mcmcresult dict; eventually won't need
            to return this once mcmcresult is packaged up nicely.
      """

      if mpirun:
            nthreads = 1
            from emcee.utils import MPIPool
            pool = MPIPool()
            if not pool.is_master():
            	pool.wait()
            	sys.exit(0)
      else: pool = None

      # Making these lists just makes later stuff easier since we now know the dtype
      source = list(np.array([source]).flatten()) # Ensure source(s) are a list
      data = list(np.array([data]).flatten())     # Same for dataset(s)
      scaleamp = list(np.array([scaleamp]).flatten())
      shiftphase = list(np.array([shiftphase]).flatten())
      modelcal = list(np.array([modelcal]).flatten())
      if len(scaleamp)==1 and len(scaleamp)<len(data): scaleamp *= len(data)
      if len(shiftphase)==1 and len(shiftphase)<len(data): shiftphase *= len(data)
      if len(modelcal)==1 and len(modelcal)<len(data): modelcal *= len(data)

      # emcee isn't very flexible in terms of how it gets initialized; start by
      # assembling the user-provided info into a form it likes
      ndim, p0, colnames = 0, [], []
      # Lens first
      if lens.__class__.__name__=='SIELens':
            for key in ['x','y','M','e','PA']:
                  if not vars(lens)[key]['fixed']:
                        ndim += 1
                        p0.append(vars(lens)[key]['value'])
                        colnames.append(key+'L')
      # Then source(s)
      for i,src in enumerate(source):
            if src.__class__.__name__=='GaussSource':
                  for key in ['xoff','yoff','flux','width']:
                        if not vars(src)[key]['fixed']:
                              ndim += 1
                              p0.append(vars(src)[key]['value'])
                              colnames.append(key+'S'+str(i))
            elif src.__class__.__name__=='SersicSource':
                  for key in ['xoff','yoff','flux','alpha','index','axisratio','PA']:
                        if not vars(src)[key]['fixed']:
                              ndim += 1
                              p0.append(vars(src)[key]['value'])
                              colnames.append(key+'S'+str(i))                         
      # Then shear
      if shear is not None:
            for key in ['shear','shearangle']:
                  if not vars(shear)[key]['fixed']:
                        ndim += 1
                        p0.append(vars(shear)[key]['value'])
                        colnames.append(key)
      # Then flux rescaling; only matters if >1 dataset
      for i,t in enumerate(scaleamp[1:]):
            if t:
                  ndim += 1
                  p0.append(1.) # Assume 1.0 scale factor to start
                  colnames.append('ampscale_dset'+str(i+1))
      # Then phase/astrometric shift; each has two vals for a shift in x&y
      for i,t in enumerate(shiftphase[1:]):
            if t:
                  ndim += 2
                  p0.append(0.); p0.append(0.) # Assume zero initial offset
                  colnames.append('astromshift_x_dset'+str(i+1))
                  colnames.append('astromshift_y_dset'+str(i+1))

      # Get any model-cal parameters set up. The process involves some expensive
      # matrix inversions, but these only need to be done once, so we'll do them
      # now and pass the results as arguments to the likelihood function. See docs
      # in modelcal.py for more info.
      for i,dset in enumerate(data):
            if modelcal[i]:
                  uniqant = np.unique(np.asarray([dset.ant1,dset.ant2]).flatten())
                  dPhi_dphi = np.zeros((uniqant.size-1,dset.u.size))
                  for j in range(1,uniqant.size):
                        dPhi_dphi[j-1,:]=(dset.ant1==uniqant[j])-1*(dset.ant2==uniqant[j])
                  C = scipy.sparse.diags((dset.sigma/dset.amp)**-2.,0)
                  F = np.dot(dPhi_dphi,C*dPhi_dphi.T)
                  Finv = np.linalg.inv(F)
                  FdPC = np.dot(-Finv,dPhi_dphi*C)
                  modelcal[i] = [dPhi_dphi,FdPC]


      # Create our lensing grid coordinates now, since those shouldn't be
      # recalculated with every call to the likelihood function
      xmap,ymap,xemit,yemit,indices = GenerateLensingGrid(data,xmax,highresbox,
                                                fieldres,emitres)

      # Calculate the uv coordinates we'll interpolate onto; only need to calculate
      # this once, so do it here.
      kmax = 0.5/((xmap[0,1]-xmap[0,0])*arcsec2rad)
      ug = np.linspace(-kmax,kmax,xmap.shape[0])

      # Calculate some distances; we only need to calculate these once.
      # This assumes multiple sources are all at same z; should be this
      # way anyway or else we'd have to deal with multiple lensing planes
      Dd = cosmo.angular_diameter_distance(lens.z).value
      Ds = cosmo.angular_diameter_distance(source[0].z).value
      Dds= cosmo.angular_diameter_distance_z1z2(lens.z,source[0].z).value

      p0 = np.array(p0)
      # Create a ball of starting points for the walkers, gaussian ball of 
      # 10% width; if initial value is 0 (eg, astrometric shift), give a small sigma
      initials = emcee.utils.sample_ball(p0,np.asarray([0.1*x if x else 0.05 for x in p0]),int(nwalkers))

      # Create the sampler object; uses calc_likelihood function defined elsewhere
      lenssampler = emcee.EnsembleSampler(nwalkers,ndim,calc_vis_lnlike,
            args = [data,lens,source,shear,Dd,Ds,Dds,ug,
                    xmap,ymap,xemit,yemit,indices,
                    sourcedatamap,scaleamp,shiftphase,modelcal],
            threads=nthreads,pool=pool)

      #return initials,lenssampler
      
      # Run burn-in phase
      print "Running burn-in... "
      #pos,prob,rstate,mus = lenssampler.run_mcmc(initials,nburn,storechain=False)
      for i,result in enumerate(lenssampler.sample(initials,iterations=nburn,storechain=False)):
            print i,'/',nburn
            pos,prob,rstate,blob = result
      
      
      lenssampler.reset()
      
      # Run actual chains
      print "Done. Running chains... "
      lenssampler.run_mcmc(pos,nstep,rstate0=rstate)
      if mpirun: pool.close()
      print "Mean acceptance fraction: ",np.mean(lenssampler.acceptance_fraction)

      # Package up the magnifications and modelcal phases; disregards nan points (where
      # we failed the prior, usu. because a periodic angle wrapped).
      blobs = lenssampler.blobs
      mus = np.asarray([[a[0] for a in l] for l in blobs]).flatten(order='F')
      bad = np.asarray([np.isnan(m) for m in mus],dtype=bool).flatten()
      mus = np.asarray(mus.flatten(),dtype=float)
      colnames.append('mu')

      
      # Assemble the output. Want to return something that contains both the MCMC chains
      # themselves, but also metadata about the run.
      mcmcresult = {}

      # keep track of git revision, for reproducibility's sake
      # if run under mpi, this will spew some scaremongering warning text,
      # but it's fine. use --mca mpi_warn_on_fork 0 in the mpirun statement to disable
      try: 
            import subprocess
            gitd = os.path.dirname(__file__)
            mcmcresult['githash'] = subprocess.check_output('git --git-dir={0:s} --work-tree={1:s} '\
                  'rev-parse HEAD'.format(gitd+'/.git',gitd),shell=True).rstrip()
      except:
            mcmcresult['githash'] = 'No repo found'
      
      
      mcmcresult['datasets'] = [dset.filename for dset in data] # Data files used

      mcmcresult['lens_p0'] = lens      # Initial params for lens,src(s),shear; also tells if fixed, priors, etc.
      mcmcresult['source_p0'] = source
      if shear: mcmcresult['shear_p0'] = shear
      
      if sourcedatamap: mcmcresult['sourcedatamap'] = sourcedatamap
      mcmcresult['xmax'] = xmax
      mcmcresult['highresbox'] = highresbox
      mcmcresult['fieldres'] = fieldres
      mcmcresult['emitres'] = emitres
      if any(scaleamp): mcmcresult['scaleamp'] = scaleamp
      if any(shiftphase): mcmcresult['shiftphase'] = shiftphase
      
      mcmcresult['chains'] = np.core.records.fromarrays(np.c_[lenssampler.flatchain[~bad],mus[~bad]].T,names=colnames)
      mcmcresult['lnlike'] = lenssampler.flatlnprobability
      
      
      # If we did any modelcal stuff, keep the antenna phase offsets here
      if any(modelcal): 
            mcmcresult['modelcal'] = [True if j else False for j in modelcal]
            dp = np.squeeze(np.asarray([[a[1] for a in l if ~np.isnan(a[0])] for l in blobs]))
            a = [x for l in dp for x in l] # Have to dick around with this if we had any nan's
            dphases = np.squeeze(np.reshape(a,(nwalkers*nstep-bad.sum(),len(data),-1),order='F'))
            if len(data) > 1: 
                  for i in range(len(data)):
                        if modelcal[i]: mcmcresult['calphases_dset'+str(i)] = np.vstack(dphases[:,i])
            else: 
                  if any(modelcal): mcmcresult['calphases_dset0'] = dphases
      
      return mcmcresult,lenssampler.flatchain,lenssampler.blobs,colnames

