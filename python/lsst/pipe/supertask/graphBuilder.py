#
# LSST Data Management System
# Copyright 2017 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#
"""
Module defining GraphBuilder class and related methods.
"""

__all__ = ['GraphBuilder']

# -------------------------------
#  Imports of standard modules --
# -------------------------------
import copy
from collections import namedtuple
from itertools import chain

# -----------------------------
#  Imports for other modules --
# -----------------------------
from .expr_parser.parserYacc import ParserYacc, ParserYaccError
from .graph import QuantumGraphNodes, QuantumGraph
import lsst.log as lsstLog
from lsst.daf.butler import Quantum

# ----------------------------------
#  Local non-exported definitions --
# ----------------------------------

_LOG = lsstLog.Log.getLogger(__name__)

# Tuple containing TaskDef, its input dataset types and output dataset types
#
# Attributes
# ----------
# taskDef : `TaskDef`
# inputs : `list` of `DatasetType`
# outputs : `list` of `DatasetType`
_TaskDatasetTypes = namedtuple("_TaskDatasetTypes", "taskDef inputs outputs")


class GraphBuilderError(Exception):
    """Base class for exceptions generated by graph builder.
    """
    pass


class UserExpressionError(GraphBuilderError):
    """Exception generated by graph builder for error in user expression.
    """

    def __init__(self, expr, exc):
        msg = "Failed to parse user expression `{}' ({})".format(expr, exc)
        GraphBuilderError.__init__(self, msg)


class OutputExistsError(GraphBuilderError):
    """Exception generated when output datasets already exist.
    """

    def __init__(self, taskName, refs):
        refs = ', '.join(str(ref) for ref in refs)
        msg = "Output datasets already exist for task {}: {}".format(taskName, refs)
        GraphBuilderError.__init__(self, msg)


# ------------------------
#  Exported definitions --
# ------------------------


class GraphBuilder(object):
    """
    GraphBuilder class is responsible for building task execution graph from
    a Pipeline.

    Parameters
    ----------
    taskFactory : `TaskFactory`
        Factory object used to load/instantiate PipelineTasks
    registry : :py:class:`daf.butler.Registry`
        Data butler instance.
    skipExisting : `bool`, optional
        If ``True`` (default) then Quantum is not created if all its outputs
        already exist, otherwise exception is raised.
    """

    def __init__(self, taskFactory, registry, skipExisting=True):
        self.taskFactory = taskFactory
        self.registry = registry
        self.dataUnits = registry._schema.dataUnits
        self.skipExisting = skipExisting

    @staticmethod
    def _parseUserQuery(userQuery):
        """Parse user query.

        Parameters
        ----------
        userQuery : `str`
            User expression string specifying data selecton.

        Returns
        -------
        `exprTree.Node` instance representing parsed expression tree.
        """
        parser = ParserYacc()
        # do parsing, this will raise exception
        try:
            tree = parser.parse(userQuery)
            _LOG.debug("parsed expression: %s", tree)
        except ParserYaccError as exc:
            raise UserExpressionError(userQuery, exc)
        return tree

    def _loadTaskClass(self, taskDef):
        """Make sure task class is loaded.

        Load task class, update task name to make sure it is fully-qualified,
        do not update original taskDef in a Pipeline though.

        Parameters
        ----------
        taskDef : `TaskDef`

        Returns
        -------
        `TaskDef` instance, may be the same as parameter if task class is
        already loaded.
        """
        if taskDef.taskClass is None:
            tClass, tName = self.taskFactory.loadTaskClass(taskDef.taskName)
            taskDef = copy.copy(taskDef)
            taskDef.taskClass = tClass
            taskDef.taskName = tName
        return taskDef

    def makeGraph(self, pipeline, originInfo, userQuery):
        """Create execution graph for a pipeline.

        Parameters
        ----------
        pipeline : :py:class:`Pipeline`
            Pipeline definition, task names/classes and their configs.
        originInfo : `DatasetOriginInfo`
            Object which provides names of the input/output collections.
        userQuery : `str`
            String which defunes user-defined selection for registry, should be
            empty or `None` if there is no restrictions on data selection.

        Returns
        -------
        :py:class:`QuantumGraph` instance.

        Raises
        ------
        `UserExpressionError` is raised when user expression cannot be parsed.
        `OutputExistsError` is raised when output datasets already exist.
        Other exceptions may be raised by underlying registry classes.
        """

        # make sure all task classes are loaded
        taskList = [self._loadTaskClass(taskDef) for taskDef in pipeline]

        # collect inputs/outputs from each task
        taskDatasets = []
        for taskDef in taskList:
            taskClass = taskDef.taskClass
            taskInputs = taskClass.getInputDatasetTypes(taskDef.config)
            taskInputs = list(taskInputs.values()) if taskInputs else []
            taskOutputs = taskClass.getOutputDatasetTypes(taskDef.config)
            taskOutputs = list(taskOutputs.values()) if taskOutputs else []
            taskDatasets.append(_TaskDatasetTypes(taskDef=taskDef,
                                                  inputs=taskInputs,
                                                  outputs=taskOutputs))

        # build initial dataset graph
        inputs, outputs = self._makeFullIODatasetTypes(taskDatasets)

        # make a graph
        return self._makeGraph(taskDatasets, inputs, outputs,
                               originInfo, userQuery)

    def _makeFullIODatasetTypes(self, taskDatasets):
        """Returns full set of input and output dataset types for all tasks.

        Parameters
        ----------
        taskDatasets : sequence of `_TaskDatasetTypes`
            Tasks with their inputs and outputs.

        Returns
        -------
        inputs : `set` of `butler.DatasetType`
            Datasets used as inputs by the pipeline.
        outputs : `set` of `butler.DatasetType`
            Datasets produced by the pipeline.
        """
        # to build initial dataset graph we have to collect info about all
        # datasets to be used by this pipeline
        allDatasetTypes = {}
        inputs = set()
        outputs = set()
        for taskDs in taskDatasets:
            for dsType in taskDs.inputs:
                inputs.add(dsType.name)
                allDatasetTypes[dsType.name] = dsType
            for dsType in taskDs.outputs:
                outputs.add(dsType.name)
                allDatasetTypes[dsType.name] = dsType

        # remove outputs from inputs
        inputs -= outputs

        inputs = set(allDatasetTypes[name] for name in inputs)
        outputs = set(allDatasetTypes[name] for name in outputs)
        return (inputs, outputs)

    def _makeGraph(self, taskDatasets, inputs, outputs, originInfo, userQuery):
        """Make QuantumGraph instance.

        Parameters
        ----------
        taskDatasets : sequence of `_TaskDatasetTypes`
            Tasks with their inputs and outputs.
        inputs : `set` of `DatasetType`
            Datasets which should already exist in input repository
        outputs : `set` of `DatasetType`
            Datasets which will be created by tasks
        originInfo : `DatasetOriginInfo`
            Object which provides names of the input/output collections.
        userQuery : `str`
            String which defunes user-defined selection for registry, should be
            empty or `None` if there is no restrictions on data selection.

        Returns
        -------
        `QuantumGraph` instance.
        """
        parsedQuery = self._parseUserQuery(userQuery or "")
        expr = None if parsedQuery is None else str(parsedQuery)
        rows = self.registry.selectDataUnits(originInfo, expr, inputs, outputs)

        # store result locally for multi-pass algorithm below
        # TODO: change it to single pass
        unitVerse = []
        for row in rows:
            _LOG.debug("row: %s", row)
            unitVerse.append(row)

        # Next step is to group by task quantum units
        qgraph = QuantumGraph()
        for taskDss in taskDatasets:
            taskQuantaInputs = {}    # key is the quantum dataId (as tuple)
            taskQuantaOutputs = {}   # key is the quantum dataId (as tuple)
            qlinks = []
            for dataUnitName in taskDss.taskDef.config.quantum.units:
                dataUnit = self.dataUnits[dataUnitName]
                qlinks += dataUnit.link
            _LOG.debug("task %s qunits: %s", taskDss.taskDef.label, qlinks)

            # some rows will be non-unique for subset of units, create
            # temporary structure to remove duplicates
            for row in unitVerse:
                qkey = tuple((col, row.dataId[col]) for col in qlinks)
                _LOG.debug("qkey: %s", qkey)

                def _dataRefKey(dataRef):
                    return tuple(sorted(dataRef.dataId.items()))

                qinputs = taskQuantaInputs.setdefault(qkey, {})
                for dsType in taskDss.inputs:
                    dataRefs = qinputs.setdefault(dsType, {})
                    dataRef = row.datasetRefs[dsType]
                    dataRefs[_dataRefKey(dataRef)] = dataRef
                    _LOG.debug("add input dataRef: %s %s", dsType.name, dataRef)

                qoutputs = taskQuantaOutputs.setdefault(qkey, {})
                for dsType in taskDss.outputs:
                    dataRefs = qoutputs.setdefault(dsType, {})
                    dataRef = row.datasetRefs[dsType]
                    dataRefs[_dataRefKey(dataRef)] = dataRef
                    _LOG.debug("add output dataRef: %s %s", dsType.name, dataRef)

            # all nodes for this task
            quanta = []
            for qkey in taskQuantaInputs:
                # taskQuantaInputs and taskQuantaOutputs have the same keys
                _LOG.debug("make quantum for qkey: %s", qkey)
                quantum = Quantum(run=None, task=None)

                # add all outputs, but check first that outputs don't exist
                outputs = list(chain.from_iterable(dataRefs.values()
                                                   for dataRefs in taskQuantaOutputs[qkey].values()))
                for ref in outputs:
                    _LOG.debug("add output: %s", ref)
                if self.skipExisting and all(ref.id is not None for ref in outputs):
                    _LOG.debug("all output dataRefs already exist, skip quantum")
                    continue
                if any(ref.id is not None for ref in outputs):
                    # some outputs exist, can't override them
                    raise OutputExistsError(taskDss.taskDef.taskName, outputs)
                for ref in outputs:
                    quantum.addOutput(ref)

                # add all inputs
                for dataRefs in taskQuantaInputs[qkey].values():
                    for ref in dataRefs.values():
                        quantum.addPredictedInput(ref)
                        _LOG.debug("add input: %s", ref)

                quanta.append(quantum)

            qgraph.append(QuantumGraphNodes(taskDss.taskDef, quanta))

        return qgraph
