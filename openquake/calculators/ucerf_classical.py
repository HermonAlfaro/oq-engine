# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2015-2017 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.
import os
import h5py
import numpy as np
import copy
import time
import logging
import functools
from datetime import datetime

from openquake.baselib.performance import Monitor
from openquake.baselib.general import DictArray, AccumDict
from openquake.baselib import parallel
from openquake.hazardlib.probability_map import ProbabilityMap
from openquake.hazardlib.calc.hazard_curve import pmap_from_grp, poe_map
from openquake.hazardlib.calc.filters import SourceFilter
from openquake.hazardlib.gsim.base import ContextMaker
from openquake.hazardlib import valid, nrml
from openquake.commonlib import source, readinput, config
from openquake.hazardlib.sourceconverter import SourceConverter

from openquake.calculators import base, classical
from openquake.calculators.ucerf_event_based import (
    UCERFControl, DEFAULT_TRT, UcerfSource)
# FIXME: the counting of effective ruptures has to be revised completely


def convert_UCERFSource(self, node):
    """
    Converts the Ucerf Source node into an SES Control object
    """
    dirname = os.path.dirname(self.fname)  # where the source_model_file is
    source_file = os.path.join(dirname, node["filename"])
    if "startDate" in node.attrib and "investigationTime" in node.attrib:
        # Is a time-dependent model - even if rates were originally
        # poissonian
        # Verify that the source time span is the same as the TOM time span
        inv_time = float(node["investigationTime"])
        if inv_time != self.tom.time_span:
            raise ValueError("Source investigation time (%s) is not "
                             "equal to configuration investigation time "
                             "(%s)" % (inv_time, self.tom.time_span))
        start_date = datetime.strptime(node["startDate"], "%d/%m/%Y")
    else:
        start_date = None
    return UCERFControl(
        source_file,
        node["id"],
        self.tom.time_span,
        start_date,
        float(node["minMag"]),
        npd=self.convert_npdist(node),
        hdd=self.convert_hpdist(node),
        aspect=~node.ruptAspectRatio,
        upper_seismogenic_depth=~node.pointGeometry.upperSeismoDepth,
        lower_seismogenic_depth=~node.pointGeometry.lowerSeismoDepth,
        msr=valid.SCALEREL[~node.magScaleRel](),
        mesh_spacing=self.rupture_mesh_spacing,
        trt=node["tectonicRegion"])
SourceConverter.convert_UCERFSource = convert_UCERFSource


def _hazard_curves_per_rupture_subset(
        rupset_idx, ucerf_source, sites, imtls, cmaker,
        truncation_level=None, monitor=Monitor()):
    """
    Calculates the probabilities of exceedence from a set of rupture indices
    """
    imtls = DictArray(imtls)
    ctx_mon = monitor('making contexts', measuremem=False)
    pne_mon = monitor('computing poes', measuremem=False)
    pmap = ProbabilityMap(len(imtls.array), len(cmaker.gsims))
    pmap.calc_times = []
    pmap.grp_id = ucerf_source.src_group_id
    nsites = len(sites)
    ucerf_source.rupset_idx = rupset_idx
    ucerf_source.num_ruptures = len(rupset_idx)
    pmap.eff_ruptures = {pmap.grp_id: ucerf_source.num_ruptures}
    # NB: the effective ruptures can be less, some may have zero probability
    t0 = time.time()
    upmap = poe_map(ucerf_source, sites, imtls, cmaker,
                    truncation_level, ctx_mon, pne_mon)
    pmap |= upmap
    pmap.calc_times.append(
        (ucerf_source.source_id, nsites, time.time() - t0))
    return pmap


def ucerf_classical_hazard_by_rupture_set(
        rupset_idx, ucerf_source, src_filter, gsims, monitor):
    """
    :param rupset_idx:
        indices of the rupture sets
    :param ucerf_source:
        an object taking the place of a source for UCERF
    :param src_filter:
        a source filter returning the sites affected by the source
    :param gsims:
        a list of GSIMs
    :param monitor:
        a monitor instance
    :returns:
        a ProbabilityMap
    """
    truncation_level = monitor.oqparam.truncation_level
    imtls = monitor.oqparam.imtls
    max_dist = src_filter.integration_distance[DEFAULT_TRT]
    rupset_idx, s_sites = \
        ucerf_source.filter_sites_by_distance_from_rupture_set(
            rupset_idx, src_filter.sitecol, max_dist)
    if len(s_sites):
        ucerf_source.src_filter = src_filter  # so that .iter_ruptures() work
        cmaker = ContextMaker(gsims, max_dist)
        pm = _hazard_curves_per_rupture_subset(
            rupset_idx, ucerf_source, s_sites, imtls, cmaker,
            truncation_level, monitor=monitor)
    else:  # return an empty probability map
        pm = ProbabilityMap(len(imtls.array), len(gsims))
        pm.calc_times = []  # TODO: fix .calc_times
        pm.eff_ruptures = {ucerf_source.src_group_id: 0}
        pm.grp_id = ucerf_source.src_group_id
    return pm
ucerf_classical_hazard_by_rupture_set.shared_dir_on = config.SHARED_DIR_ON


def ucerf_classical_hazard_by_branch(ucerf_source, src_filter, gsims, monitor):
    """
    :param ucerf_source:
        a source-like object for the UCERF model
    :param src_filter:
        a filter returning the sites affected by the source
    :param gsims:
        a list of GSIMs
    :param monitor:
        a monitor instance
    :returns:
        a ProbabilityMap
    """
    truncation_level = monitor.oqparam.truncation_level
    imtls = monitor.oqparam.imtls
    # Two step process here - the first generates the hazard curves from
    # the rupture sets
    # Apply the initial rupture to site filtering
    rupset_idx = ucerf_source.get_rupture_indices()
    pm = ucerf_classical_hazard_by_rupture_set(
        rupset_idx, ucerf_source, src_filter, gsims, monitor)
    logging.info('Branch %s', ucerf_source.source_id)
    # Get the background point sources
    background_sids = ucerf_source.get_background_sids(src_filter)
    bckgnd_sources = ucerf_source.get_background_sources(background_sids)
    if bckgnd_sources:
        bckgnd_sources[0].src_group_id = ucerf_source.src_group_id
        pmap = pmap_from_grp(
            bckgnd_sources, src_filter, imtls, gsims, truncation_level,
            (), monitor=monitor)
        pm |= pmap
        pm.eff_ruptures += AccumDict(pmap.eff_ruptures)
    # TODO: should I add a .calc_times attribute?
    return pm
ucerf_classical_hazard_by_branch.shared_dir_on = config.SHARED_DIR_ON


@base.calculators.add('ucerf_psha')
class UcerfPSHACalculator(classical.PSHACalculator):
    """
    UCERF classical calculator.
    """
    core_task = ucerf_classical_hazard_by_branch
    is_stochastic = False

    def pre_execute(self):
        """
        parse the logic tree and source model input
        """
        logging.warn('%s is still experimental', self.__class__.__name__)
        self.sitecol = readinput.get_site_collection(self.oqparam)
        self.gsim_lt = readinput.get_gsim_lt(self.oqparam, [DEFAULT_TRT])
        self.smlt = readinput.get_source_model_lt(self.oqparam)
        parser = nrml.SourceModelParser(
            SourceConverter(self.oqparam.investigation_time,
                            self.oqparam.rupture_mesh_spacing))
        [self.src_group] = parser.parse_src_groups(
            self.oqparam.inputs["source_model"])
        source_models = []
        for sm in self.smlt.gen_source_models(self.gsim_lt):
            sg = copy.copy(self.src_group)
            sg.id = sm.ordinal
            sm.src_groups = [sg]
            # sm.path[0]=ltbrTrueMean, sm.name=FM0_0/MEANFS/MEANMSR/MeanRates
            sg.sources = [UcerfSource(sg[0], sm.ordinal, sm.path[0], sm.name)]
            source_models.append(sm)
        self.csm = source.CompositeSourceModel(
            self.gsim_lt, self.smlt, source_models, set_weight=True)
        self.rlzs_assoc = self.csm.info.get_rlzs_assoc()
        self.rup_data = {}
        self.num_tiles = 1
        self.infos = {}

    def gen_args(self, source_models, monitor):
        """
        :yields: (ucerf_source, src_filter, gsims, monitor)
        """
        for sm in source_models:
            [grp] = sm.src_groups
            [ucerf_source] = grp
            ucerf_source.nsites = len(self.src_filter.sitecol)
            gsims = self.rlzs_assoc.gsims_by_grp_id[grp.id]
            self.infos[grp.id, ucerf_source.source_id] = source.SourceInfo(
                ucerf_source)
            yield ucerf_source, self.src_filter, gsims, monitor

    def execute(self):
        """
        Run in parallel `core_task(sources, sitecol, monitor)`, by
        parallelizing on the sources according to their weight and
        tectonic region type.
        """
        monitor = self.monitor.new(self.core_task.__name__)
        monitor.oqparam = oq = self.oqparam
        self.src_filter = SourceFilter(self.sitecol, oq.maximum_distance)
        acc = AccumDict({
            grp_id: ProbabilityMap(len(oq.imtls.array), len(gsims))
            for grp_id, gsims in self.rlzs_assoc.gsims_by_grp_id.items()})
        acc.calc_times = []
        acc.eff_ruptures = AccumDict()  # grp_id -> eff_ruptures
        acc.bb_dict = {}  # just for API compatibility

        if len(self.csm) > 1:
            # when multiple branches, parallelise by branch
            rup_res = parallel.Starmap(
                ucerf_classical_hazard_by_branch,
                self.gen_args(self.csm.source_models, monitor)).submit_all()
        else:
            # single branch
            gsims = self.rlzs_assoc.gsims_by_grp_id[0]
            ucerf_source = self.csm.source_models[0].src_groups[0][0]
            ucerf_source.nsites = len(self.sitecol)
            self.infos[0, ucerf_source.source_id] = source.SourceInfo(
                ucerf_source)
            logging.info('Getting the background point sources')
            with self.monitor('getting background sources', autoflush=True):
                background_sids = ucerf_source.get_background_sids(
                    self.src_filter)
                bckgnd_sources = ucerf_source.get_background_sources(
                    background_sids)

            # parallelize on the background sources, small tasks
            args = (bckgnd_sources, self.src_filter, oq.imtls,
                    gsims, self.oqparam.truncation_level, (), monitor)
            bg_res = parallel.Starmap.apply(
                pmap_from_grp, args,
                concurrent_tasks=self.oqparam.concurrent_tasks).submit_all()

            # parallelize by rupture subsets
            tasks = self.oqparam.concurrent_tasks * 2  # they are big tasks
            rup_sets = ucerf_source.get_rupture_indices()
            rup_res = parallel.Starmap.apply(
                ucerf_classical_hazard_by_rupture_set,
                (rup_sets, ucerf_source, self.src_filter, gsims, monitor),
                concurrent_tasks=tasks).submit_all()

            # compose probabilities from background sources
            for pmap in bg_res:
                acc[0] |= pmap
            self.save_data_transfer(bg_res)

        pmap_by_grp_id = functools.reduce(self.agg_dicts, rup_res, acc)
        with self.monitor('store source_info', autoflush=True):
            self.store_source_info(self.infos)
            self.save_data_transfer(rup_res)
        self.datastore['csm_info'] = self.csm.info
        self.rlzs_assoc = self.csm.info.get_rlzs_assoc(
            functools.partial(self.count_eff_ruptures, pmap_by_grp_id))
        return pmap_by_grp_id


@base.calculators.add('ucerf_classical')
class UCERFClassicalCalculator(classical.ClassicalCalculator):
    pre_calculator = 'ucerf_psha'
