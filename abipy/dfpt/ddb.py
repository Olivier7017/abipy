# coding: utf-8
"""DDB File."""
from __future__ import print_function, division, unicode_literals

import os
import tempfile
import numpy as np

from monty.collections import AttrDict
from monty.functools import lazy_property
from pymatgen.io.abinitio.tasks import AnaddbTask, TaskManager
from abipy.core.mixins import TextFile, Has_Structure
from abipy.core.symmetries import SpaceGroup
from abipy.core.structure import Structure
from abipy.core.kpoints import KpointList
from abipy.core.tensor import Tensor
from abipy.iotools import ETSF_Reader
from abipy.abio.inputs import AnaddbInput

import logging
logger = logging.getLogger(__name__)


class TaskException(Exception):
    """
    Exceptions raised when we try to execute :class:`AnaddbTask` in the :class:`DdbFile` methods

    A `TaskException` has a reference to the task and to the :class:`EventsReport` that contains
    the error messages of the run.
    """
    def __init__(self, *args, **kwargs):
        self.task, self.report = kwargs.pop("task"), kwargs.pop("report")
        super(TaskException, self).__init__(*args, **kwargs)

    def __str__(self):
        lines = ["\nworkdir = %s" % self.task.workdir]
        app = lines.append

        if self.report.errors: 
            app("Found %d errors" % len(self.report.errors))
            lines += [str(err) for err in self.report.errors]

        return "\n".join(lines)


class DdbFile(TextFile, Has_Structure):
    """
    This object provides an interface to the DDB file produced by ABINIT
    as well as methods to compute phonon band structures, phonon DOS, thermodinamical properties ...
    """
    @lazy_property
    def structure(self):
        structure = Structure.from_abivars(**self.header)
        # Add Spacegroup (needed in guessed_ngkpt)
        # FIXME: has_timerev is always True
        spgid, has_timerev, h = 0, True, self.header
        structure.set_spacegroup(SpaceGroup(spgid, h.symrel, h.tnons, h.symafm, has_timerev))
        return structure

    @lazy_property
    def header(self):
        """
        Dictionary with the values reported in the header section. 
        Use ddb.header.ecut to access its values
        """
        return self._parse_header()

    def _parse_header(self):
        """Parse the header sections. Returns :class:`AttrDict` dictionary."""
        #ixc         7
        #kpt  0.00000000000000D+00  0.00000000000000D+00  0.00000000000000D+00
        #     0.25000000000000D+00  0.00000000000000D+00  0.00000000000000D+00
        self.seek(0)
        keyvals = []
        for i, line in enumerate(self):
            line = line.strip()
            if not line: continue
            if "Version" in line:
                # +DDB, Version number    100401
                version = int(line.split()[-1])

            if line in ("Description of the potentials (KB energies)",
                        "No information on the potentials yet"):
                # Skip section with psps info.
                break

            # header starts here
            if i >= 6:
                # Python does not support exp format with D 
                line = line.replace("D+", "E+").replace("D-", "E-")
                tokens = line.split()
                key = None
                try:
                    float(tokens[0])
                    parse = float if "." in tokens[0] else int
                    keyvals[-1][1].extend(map(parse, tokens))
                except ValueError:
                    # We have a new key
                    key = tokens.pop(0)
                    parse = float if "." in tokens[0] else int
                    keyvals.append((key, map(parse, tokens)))

        h = AttrDict(version=version)
        for key, value in keyvals:
            if len(value) == 1: value = value[0]
            h[key] = value

        # Convert to array. Note that znucl is converted into integer
        # to avoid problems with pymatgen routines that expect integral Z
        # This of course will break any code for alchemical mixing.
        arrays = {
            "kpt": dict(shape=(h.nkpt, 3), dtype=np.double),
            "rprim": dict(shape=(3, 3), dtype=np.double),
            "symrel": dict(shape=(h.nsym, 3, 3), dtype=np.int),
            "tnons": dict(shape=(h.nsym, 3), dtype=np.double),
            "xred":  dict(shape=(h.natom, 3), dtype=np.double),
            "znucl": dict(shape=(-1,), dtype=np.int),
        }

        for k, ainfo in arrays.items():
            h[k] = np.reshape(np.array(h[k], dtype=ainfo["dtype"]), ainfo["shape"])

        # Transpose symrel because Abinit write matrices by colums.
        h.symrel = np.array([s.T for s in h.symrel])
        
        return h

    def _read_qpoints(self):
        """Read the list q-points from the DDB file. Returns `ndarray`"""
        # 2nd derivatives (non-stat.)  - # elements :      36
        # qpt  2.50000000E-01  0.00000000E+00  0.00000000E+00   1.0

        # Since there are multiple occurrences of qpt in the DDB file
        # we use seen to remove duplicates.
        self.seek(0)
        tokens, seen = [], set()

        for line in self:
            line = line.strip()
            if line.startswith("qpt") and line not in seen:
                seen.add(line)
                tokens.append(line.replace("qpt", ""))

        qpoints, weights = [], []
        for tok in tokens:
            nums = list(map(float, tok.split()))
            qpoints.append(nums[:3])
            weights.append(nums[3])

        return np.reshape(qpoints, (-1,3))

    @lazy_property
    def qpoints(self):
        """:class:`KpointList` object with the list of q-points in reduced coordinates."""
        frac_coords = self._read_qpoints()
        return KpointList(self.structure.reciprocal_lattice, frac_coords, weights=None, names=None)

    def qindex(self, qpoint):
        """
        The index of the q-point in the internal list of k-points.
        Accepts: :class:`qpoint` instance or integer.
        """
        if isinstance(qpoint, int):
            return qpoint
        else:
            return self.qpoints.index(qpoint)

    @lazy_property
    def guessed_ngqpt(self):
        """
        This function tries to figure out the value of ngqpt from the list of 
        points reported in the DDB file.

        .. warning::
            
            The mesh may not be correct if the DDB file contains points belonging 
            to different meshes and/or the Q-mesh is shifted.
        """
        # Build the union of the stars of the q-points.
        all_qpoints = np.empty((len(self.qpoints) * len(self.structure.spacegroup), 3))
        count = 0
        for qpoint in self.qpoints:
            for op in self.structure.spacegroup:
                all_qpoints[count] = op.rotate_k(qpoint.frac_coords, wrap_tows=False)
                count += 1

        # Replace zeros with np.inf
        for q in all_qpoints: 
            q[q == 0] = np.inf

        # Compute the minimum of the fractional coordinates along the 3 directions and invert
        #print(all_qpoints)
        smalls = np.abs(all_qpoints).min(axis=0)
        smalls[smalls == 0] = 1
        ngqpt = np.rint(1 / smalls)
        ngqpt[ngqpt == 0] = 1
        #print("smalls: ", smalls, "ngqpt", ngqpt)

        return np.array(ngqpt, dtype=np.int)

    @property
    def params(self):
        """Dictionary with the parameters that are usually tested for convergence."""
        return {k: v for k, v in self.header.items() if k in ("nkpt", "nsppol", "ecut", "tsmear", "ixc")}

    def anaget_phmodes_at_qpoint(self, qpoint=None, asr=2, chneut=1, dipdip=1, 
                                 workdir=None, manager=None, verbose=0):
        """
        Execute anaddb to compute phonon modes at the given q-point.

        Args:
            qpoint: Reduced coordinates of the qpoint where phonon modes are computed
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            workdir: Working directory. If None, a temporary directory is created.
            manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
            verbose: verbosity level. Set it to a value > 0 to get more information

        Return:
            :class:`PhononBands` object.
        """
        if qpoint is None:
            qpoint = self.qpoints[0] 
            if len(self.qpoints) != 1:
                raise ValueError("%s contains %s qpoints and the choice is ambiguous.\n" 
                                 "Please specify the qpoint." % (self, len(self.qpoints)))

        # Check if qpoint is in the DDB.
        try:
            self.qindex(qpoint)
        except:
            raise ValueError("input qpoint %s not in ddb.qpoints:%s\n" % (qpoint, self.qpoints))

        inp = AnaddbInput.modes_at_qpoint(self.structure, qpoint, asr=asr, chneut=chneut, dipdip=dipdip)

        task = AnaddbTask.temp_shell_task(inp, ddb_node=self.filepath, workdir=workdir, manager=manager)

        if verbose: 
            print("ANADDB INPUT:\n", inp)
            print("workdir:", task.workdir)

        # Run the task here
        task.start_and_wait(autoparal=False)
        report = task.get_event_report()
        if not report.run_completed:
            raise TaskException(task=task, report=report)

        with task.open_phbst() as ncfile:
            return ncfile.phbands

    #def anaget_phbands(self, ngqpt=None, ndivsm=20, asr=2, chneut=1, dipdip=1, workdir=None, manager=None, verbose=0, **kwargs):
    #def anaget_phdos(self, ngqpt=None, nqsmall=10, asr=2, chneut=1, dipdip=1, dos_method="tetra" workdir=None, manager=None, verbose=0, **kwargs):

    def anaget_phbands_and_dos(self, ngqpt=None, ndivsm=20, nqsmall=10, asr=2, chneut=1, dipdip=1, dos_method="tetra",
                             workdir=None, manager=None, verbose=0, plot=True):
        """
        Execute anaddb to compute the phonon band structure and the phonon DOS

        Args:
            ngqpt: Number of divisions for the q-mesh in the DDB file. Auto-detected if None (default)
            asr, chneut, dipdp: Anaddb input variable. See official documentation.
            workdir: Working directory. If None, a temporary directory is created.
            manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
            verbose: verbosity level. Set it to a value > 0 to get more information
        """
        if ngqpt is None: ngqpt = self.guessed_ngqpt

        inp = AnaddbInput.phbands_and_dos(
            self.structure, ngqpt=ngqpt, ndivsm=ndivsm, nqsmall=nqsmall, 
            q1shft=(0,0,0), qptbounds=None, asr=asr, chneut=chneut, dipdip=dipdip, dos_method=dos_method)

        task = AnaddbTask.temp_shell_task(inp, ddb_node=self.filepath, workdir=workdir, manager=manager)

        if verbose: 
            print("ANADDB INPUT:\n", inp)
            print("workdir:", task.workdir)

        # Run the task here.
        task.start_and_wait(autoparal=False)

        report = task.get_event_report()
        if not report.run_completed:
            raise TaskException(task=task, report=report)

        with task.open_phbst() as phbst_ncfile, task.open_phdos() as phdos_ncfile:
            phbands, phdos = phbst_ncfile.phbands, phdos_ncfile.phdos
            if plot:
                phbands.plot_with_phdos(phdos, title="Phonon bands and DOS of %s" % self.structure.formula)

            return phbands, phdos

    def anaget_emacro_and_becs(self, chneut=1, workdir=None, manager=None, verbose=0):
        """
        Call anaddb to compute the macroscopic dielectric tensor and the Born effective charges.

        Args:
            chneut: Anaddb input variable. See official documentation.
            manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
            verbose: verbosity level. Set it to a value > 0 to get more information

        Return:
            emacro: (3, 3) `ndarrary`
            becs: (3, 3, natom) `ndarray`
        """
        inp = AnaddbInput(self.structure, anaddb_kwargs={"chneut": chneut})

        task = AnaddbTask.temp_shell_task(inp, ddb_node=self.filepath, workdir=workdir, manager=manager)

        if verbose: 
            print("ANADDB INPUT:\n", inp)
            print("workdir:", task.workdir)

        # Run the task here.
        task.start_and_wait(autoparal=False)

        report = task.get_event_report()
        if not report.run_completed:
            raise TaskException(task=task, report=report)

        # Read data from the netcdf output file produced by anaddb.
        with ETSF_Reader(os.path.join(task.workdir, "anaddb.nc")) as r:
            structure = r.read_structure()

            # Frac or cart?
            emacro_mat = r.read_value("emacro")
            emacro = Tensor(emacro_mat, structure.lattice, space="r")

            becs_arr = r.read_value("becs")
            becs = Becs(becs_arr, structure, order="f")

            return emacro, becs

    #def anaget_thermo(self, nqsmall, ngqpt=None, workdir=None, manager=None, verbose=0):
    #    """
    #    Execute anaddb to compute thermodinamical properties.

    #    Args:
    #        ngqpt: Number of divisions for the q-mesh in the DDB file. Auto-detected if it is None
    #        asr, chneut, dipdp: Anaddb input variable. See official documentation.
    #        workdir: Working directory. If None, a temporary directory is created.
    #        manager: :class:`TaskManager` object. If None, the object is initialized from the configuration file
    #        verbose: verbosity level. Set it to a value > 0 to get more information
    #        kwargs: Additional variables you may want to pass to Anaddb.
    #    """
    #    if ngqpt is None: ngqpt = self.guessed_ngqpt

    #    inp = AnaddbInput.thermo(self.structure, ngqpt, nqsmall, q1shft=(0, 0, 0), nchan=1250, nwchan=5, thmtol=0.5,
    #           ntemper=199, temperinc=5, tempermin=5., asr=2, chneut=1, dipdip=1, ngrids=10)

    #    task = AnaddbTask.temp_shell_task(inp, self.filepath, workdir=workdir, manager=manager.to_shell_manager(mpi_procs=1))

    #    if verbose: 
    #        print("ANADDB INPUT:\n", inp)
    #        print("workdir:", task.workdir)

    #    task.start_and_wait(autoparal=False)

    #    report = task.get_event_report()
    #    if not report.run_completed:
    #        raise TaskException(task=task, report=report)

    #    def converge_phdos(self, nqsmall_slice):
    #        from monty.dev import get_ncpus
    #        self.num_cores = get_num_cpus() if num_cores is None else num_cores
    #        doses = []
    #        for nqs in nsmall_range:
    #           #self.ddb.anaget_phbands_and_dos(ngqpt=None, ndivsm=20, nqsmall=10, workdir=None, manager=self.manager)
    #           doses.append(phdos)
    #
    #        # Compare last three phonon DOSes.
    #        # Be careful here because the DOS may be defined on different frequency meshes
    #        last_mesh = doses[-1].mesh
    #        converged = False
    #         for dos in doses[:-1]:
    #            dos = dos.spline_on_mesh(last_mesh)
    #            diffs.append(dos - doses[-1])
    #
    #        if converged:
    #            return collections.namedtuple("results", "phdos ngsmall plotter")
    #                (phdos=phdos, nqsmall=nqsmall, plotter=plotter)
    #        else:
    #            raise self.Error("Cannot converge the DOS wrt nqsmall")



class Becs(Has_Structure):
    """
    This object stores the Born effective charges and provides analysis tools
    """
    def __init__(self, becs_arr, structure, order="c"):
        """

        Args:
            becs_arr: (3, 3, natom) array wit the Born effective charges.
            structure: Structure object.
            order: "f" if becs_arr is in Fortran order.
        """
        assert len(becs_arr) == len(structure)
        self._structure = structure

        # Here we
        self.becs = np.empty((len(structure), 3, 3))
        for i, bec in enumerate(becs_arr):
            mat = becs_arr[i]
            if order == "f": mat = mat.T
            self.becs[i] = mat

    @property
    def structure(self):
        return self._structure

    def __repr__(self):
        return self.to_string()

    def __str__(self):
        return self.to_string()

    def to_string(self):
        lines = [str(self.structure)]
        app = lines.append
        app("")

        for site, bec in zip(self.structure, self.becs):
            # TODO: why PeriodicSite.__str__ does not give the frac_coords?
            #print(type(site))
            app("bec at site: %s" % (site))
            app(str(bec))
            app("")

        return "\n".join(lines)

    def check_sumrule(self):
        becs_atomsum = self.becs.sum(axis=0)
        print("Born effective charge neutrality sum-rule:")
        print(bec_atomsum)