# Copyright 2022 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List

import pandas as pd

from .input_column_selector import InputColumnSelector
from ..core import register_optimization_rule
from ...core import (
    OptimizationRecord,
    OptimizationRecordType,
    OptimizationRule,
)
from .....dataframe.core import (
    parse_index,
    BaseSeriesData,
    BaseDataFrameData,
)
from .....dataframe.datasource.core import ColumnPruneSupportedDataSourceMixin
from .....dataframe.groupby.aggregation import DataFrameGroupByAgg
from .....dataframe.indexing.getitem import DataFrameIndex
from .....dataframe.merge import DataFrameMerge
from .....typing import EntityType
from .....utils import implements

OPTIMIZABLE_OP_TYPES = (DataFrameMerge, DataFrameGroupByAgg)


@register_optimization_rule()
class ColumnPruningRule(OptimizationRule):
    context = {}

    def _get_selected_columns(self, entity: EntityType):
        """
        Get pruned columns of the given entity.
        """
        successors = self._get_successors(entity)
        if successors:
            return set().union(
                *[self.context[successor][entity] for successor in successors]
            )
        else:
            return self._get_all_columns(entity)

    @staticmethod
    def _get_all_columns(entity: EntityType):
        """
        Return all the columns of given entity. If the given entity is neither BaseDataFrameData nor BaseSeriesData,
        None will be returned.
        """
        if isinstance(entity, BaseDataFrameData) and entity.dtypes is not None:
            return set(entity.dtypes.index)
        elif isinstance(entity, BaseSeriesData):
            return {entity.name}
        else:
            return None

    def _get_successors(self, entity: EntityType):
        """
        Get successors of the given entity.

        Column pruning is available only when every successor is available for column pruning (i.e. appears in the
        context).
        """
        successors = list(self._graph.successors(entity))
        if all(successor in self.context for successor in successors):
            return successors
        else:
            return []

    def _select_columns(self):
        """
        Select required columns for each entity in the graph.
        """
        for entity in self._graph.topological_iter(reverse=True):
            if self._is_skipped_type(entity):
                continue
            self.context[entity] = InputColumnSelector.select_input_columns(
                entity, self._get_selected_columns(entity)
            )

    def _insert_getitem_nodes(self):
        pruned_nodes = []
        datasource_nodes = []
        node_list = list(self._graph.topological_iter())
        for entity in node_list:
            if self._is_skipped_type(entity):
                continue

            op = entity.op
            selected_columns = self._get_selected_columns(entity)
            if isinstance(op, ColumnPruneSupportedDataSourceMixin) and set(
                selected_columns
            ) != self._get_all_columns(entity):
                op.set_pruned_columns(list(selected_columns))
                self.effective = True
                pruned_nodes.append(entity)
                datasource_nodes.append(entity)
                continue

            if isinstance(op, OPTIMIZABLE_OP_TYPES):
                predecessors = list(self._graph.predecessors(entity))
                for predecessor in predecessors:
                    if (
                        self._is_skipped_type(predecessor)
                        or predecessor in datasource_nodes
                        # if the group by key is a series, no need to do column pruning
                        or isinstance(predecessor, BaseSeriesData)
                    ):
                        continue

                    pruned_columns = list(self.context[entity][predecessor])
                    if set(pruned_columns) == self._get_all_columns(predecessor):
                        continue

                    # new node init
                    new_node_op = DataFrameIndex(
                        col_names=pruned_columns,
                    )
                    new_params = predecessor.params.copy()
                    new_params["shape"] = (
                        new_params["shape"][0],
                        len(pruned_columns),
                    )
                    new_params["dtypes"] = new_params["dtypes"][pruned_columns]
                    new_params["columns_value"] = parse_index(
                        new_params["dtypes"].index, store_data=True
                    )
                    new_node = new_node_op.new_dataframe(
                        [predecessor], **new_params
                    ).data

                    # update context
                    del self.context[entity][predecessor]
                    self.context[new_node] = {predecessor: pruned_columns}
                    self.context[entity][new_node] = pruned_columns

                    # change edges and nodes
                    self._graph.remove_edge(predecessor, entity)
                    self._graph.add_node(new_node)
                    self._graph.add_edge(predecessor, new_node)
                    self._graph.add_edge(new_node, entity)

                    self._records.append_record(
                        OptimizationRecord(
                            predecessor, new_node, OptimizationRecordType.new
                        )
                    )
                    # update inputs
                    entity.inputs[entity.inputs.index(predecessor)] = new_node
                    self.effective = True
                    pruned_nodes.extend([predecessor])
        return pruned_nodes

    def _update_tileable_params(self, pruned_nodes: List[EntityType]):
        # change dtypes and columns_value
        queue = [n for n in pruned_nodes]
        affected_nodes = set()
        while len(queue) > 0:
            node = queue.pop(0)
            if isinstance(node.op, ColumnPruneSupportedDataSourceMixin):
                affected_nodes.add(node)
            for successor in self._graph.successors(node):
                if successor not in affected_nodes:
                    queue.append(successor)
                    if not self._is_skipped_type(successor):
                        affected_nodes.add(successor)

        for node in affected_nodes:
            selected_columns = self._get_selected_columns(node)
            if isinstance(node, BaseDataFrameData) and set(selected_columns) != set(
                node.dtypes.index
            ):
                new_dtypes = pd.Series(
                    dict(
                        (col, dtype)
                        for col, dtype in node.dtypes.iteritems()
                        if col in selected_columns
                    )
                )
                new_columns_value = parse_index(new_dtypes.index, store_data=True)
                node._dtypes = new_dtypes
                node._columns_value = new_columns_value
                node._shape = (node.shape[0], len(new_dtypes))

    @implements(OptimizationRule.apply)
    def apply(self):
        self._select_columns()
        pruned_nodes = self._insert_getitem_nodes()
        self._update_tileable_params(pruned_nodes)

    @staticmethod
    def _is_skipped_type(entity: EntityType) -> bool:
        """
        If an entity is not a DataFrame or a Series, do not handle that.
        Parameters
        ----------
        entity

        Returns
        -------

        """
        return not isinstance(entity, (BaseSeriesData, BaseDataFrameData))
