import logging
import copy

import networkx as nx

from rabix.common.protocol import WrapperJob, Outputs, Resources
from rabix.common.util import rnd_name
from rabix.runtime.models import Pipeline

log = logging.getLogger(__name__)


class Task(object):
    QUEUED, READY, RUNNING, FINISHED, CANCELED, FAILED = 'queued', 'ready', 'running', 'finished', 'canceled', 'failed'

    def __init__(self, task_name='', resources=None, arguments=None):
        self.status = Task.QUEUED
        self.task_id = None  # Determined by TaskDAG
        self.task_name = task_name
        self.resources = resources
        self.arguments = arguments
        self.result = None
        self.is_replacement = False

    __str__ = __unicode__ = __repr__ = lambda self: '%s[%s]' % (self.__class__.__name__, self.task_id)

    def replacement(self, resources, arguments):
        replacement = copy.deepcopy(self)
        replacement.status = Task.QUEUED
        replacement.resources = resources
        replacement.arguments = self._replace_wrapper_job_with_task(arguments)
        replacement.task_id = None
        replacement.result = None
        replacement.is_replacement = True
        return replacement

    def _replace_wrapper_job_with_task(self, obj):
        """ Traverses arguments, creates replacement tasks in place of wrapper jobs """
        if isinstance(obj, list):
            for ndx, item in enumerate(obj):
                if isinstance(item, WrapperJob):
                    obj[ndx] = self.replacement(resources=item.resources, arguments=item.args)
                elif isinstance(obj, (dict, list)):
                    obj[ndx] = self._replace_wrapper_job_with_task(item)
        if isinstance(obj, dict):
            for key, val in obj.iteritems():
                if isinstance(val, WrapperJob):
                    obj[key] = self.replacement(resources=val.resources, arguments=val.args)
                elif isinstance(val, (dict, list)):
                    obj[key] = self._replace_wrapper_job_with_task(val)
        return obj

    def iter_deps(self):
        """ Yields (path, task) for all tasks somewhere in the arguments tree. """
        if isinstance(self.arguments, Task):
            yield self.arguments
        for path, job in filter(None, Task._recursive_traverse([], self.arguments)):
            yield path, job

    @staticmethod
    def _recursive_traverse(path, obj):
        if not isinstance(obj, (dict, list)):
            return
        gen = obj.iteritems() if isinstance(obj, dict) else enumerate(obj)
        for k, v in gen:
            child_path = path + [k]
            if isinstance(v, Task):
                yield child_path, v
            elif isinstance(v, (dict, list)):
                tasks = filter(None, Task._recursive_traverse(child_path, v))
                for task in tasks:
                    yield task
            else:
                yield None


class AppTask(Task):
    def __init__(self, app, task_name='', resources=None, arguments=None):
        super(AppTask, self).__init__(task_name, resources, arguments)
        self.app = app


class PipelineStepTask(AppTask):
    def __init__(self, app, step, task_name='', resources=None, arguments=None):
        super(PipelineStepTask, self).__init__(app, task_name, resources)
        self.step = step
        self.arguments = arguments
        if arguments is None:
            inputs = list(inp['id'] for inp in app.schema.inputs)
            params = step.get('parameters', {})
            print 'Params', params, step
            self.arguments = {'$inputs': {inp: [] for inp in inputs}, '$params': params}


class AppInstallTask(AppTask):
    pass


class InputTask(Task):
    pass


class OutputTask(Task):
    pass


class TaskDAG(object):
    def __init__(self, task_prefix=''):
        self.task_prefix = task_prefix or rnd_name()
        self.dag = nx.DiGraph()

    def get_id_for_task(self, task):
        base = '%s.%s' % (self.task_prefix, task.task_name)
        task_id, counter = base, 0
        while task_id in self.dag:
            counter += 1
            task_id = '%s.%s' % (base, counter)
        return task_id

    def add_task(self, task):
        """ Modifies task id. Returns modified task. """
        task.task_id = self.get_id_for_task(task)
        self.dag.add_node(task.task_id, task=task)
        for path, dep in task.iter_deps():
            dep = self.add_task(dep)
            self.connect(dep.task_id, task.task_id, [], path)
        return task

    def connect(self, src_id, dst_id, src_path, dst_path):
        if src_id not in self.dag or dst_id not in self.dag:
            raise ValueError('Node does not exist.')
        conns = self.dag.get_edge_data(src_id, dst_id, default={'conns': []})['conns']
        conns.append((src_path, dst_path))
        self.dag.add_edge(src_id, dst_id, conns=conns)

    def get_task(self, task_id):
        return self.dag.node[task_id]['task']

    def iter_tasks(self):
        for node in self.dag.node.itervalues():
            yield node['task']

    def resolve_task(self, task):
        """
        If successful, result is propagated downstream. Downstream tasks may get READY status.
        If result is a task, it is used as a replacement and dependency tasks are added to the graph.
        If failed, result should be an exception.
        If cancelled, result should be None.
        """
        if task.status not in (Task.FINISHED, Task.CANCELED, Task.FAILED):
            raise ValueError('Invalid resolution: %s' % task.status)
        if task.status != task.FINISHED:
            return
        if isinstance(task.result, Outputs):
            task.result = task.result.outputs
        if isinstance(task.result, WrapperJob):
            replacement = self.add_task(task.replacement(resources=task.result.resources, arguments=task.result.args))
            for n in self.dag.neighbors(task.task_id):
                self.dag.add_edge(replacement.task_id, n, **self.dag.get_edge_data(task.task_id, n))
                self.dag.remove_edge(task.task_id, n)
            return
        for dst_id in self.dag.neighbors(task.task_id):
            dst = self.get_task(dst_id)
            for src_path, dst_path in self.dag.get_edge_data(task.task_id, dst_id)['conns']:
                self.propagate(task, dst, src_path, dst_path)
            self.update_status(dst)

    def propagate(self, src, dst, src_path, dst_path):
        """" Propagates results. Possibly override to add shuttling tasks when multi node and no shared storage. """
        val = get_val_from_path(src.result, src_path)
        dst.arguments = update_on_path(dst.arguments, dst_path, val)

    def update_status(self, task):
        if task.status != Task.QUEUED:
            return task
        dep_ids = self.dag.reverse(copy=True).neighbors(task.task_id)  # TODO: Do it without copying.
        deps = [self.get_task(dep_id) for dep_id in dep_ids]
        if not deps or all(dep.status == Task.FINISHED for dep in deps):
            task.status = Task.READY
        return task

    def get_ready_tasks(self):
        return [task for task in self.iter_tasks() if self.update_status(task).status == Task.READY]

    def add_from_app(self, app, inputs):
        return self.add_from_pipeline(Pipeline.from_app(app), inputs)

    def add_from_pipeline(self, pipeline, inputs):
        inputs = inputs or {}
        for node_id, node in pipeline.nx.node.iteritems():
            if node['app'] == '$$input':
                self.add_task(InputTask(node_id, arguments=inputs.get(node_id, [])))
            elif node['app'] == '$$output':
                self.add_task(OutputTask(node_id))
            else:
                self.add_task(PipelineStepTask(node['app'], node['step'], task_name=node_id))
        for src_id, destinations in pipeline.nx.edge.iteritems():
            for dst_id, data in destinations.iteritems():
                for out_id, inp_id in data['conns']:
                    src_path = [out_id] if out_id else []
                    dst_path = ['$inputs', inp_id] if inp_id else []
                    self.connect('%s.%s' % (self.task_prefix, src_id), '%s.%s' % (self.task_prefix, dst_id),
                                 src_path, dst_path)

    def add_install_tasks(self, pipeline_or_app):
        for app_id, app in pipeline_or_app.apps.iteritems():
            self.add_task(AppInstallTask(app, task_name=app_id + '.install'))

    def get_outputs(self):
        return {task.task_name: task.result or [] for task in self.iter_tasks() if isinstance(task, OutputTask)}


def get_val_from_path(obj, path, default=None):
    if path is None:
        return None
    if not isinstance(path, list):
        raise TypeError('Path must be a list.')
    if not path:
        return obj
    curr = obj
    for chunk in path:
        try:
            curr = curr[chunk]
        except (KeyError, IndexError):
            return default
    return curr


def update_on_path(obj, path, val):
    if path is None:
        return obj
    if not isinstance(path, list):
        raise TypeError('Path must be a list.')
    if not path:
        return val
    curr = obj
    for chunk in path[:-1]:
        curr = curr[chunk]
    parent, dest = curr, path[-1]
    if isinstance(parent[dest], list):
        val = val if isinstance(val, list) else [val]
        parent[dest].extend(val)
    else:
        parent[dest] = val
    return obj


class Runner(object):
    def __init__(self, task):
        if not isinstance(task, Task):
            raise TypeError('Expected Task, got %s' % type(task))
        self.task = task

    def run(self):
        return self.task.arguments

    def get_requirements(self):
        return self.task.resources or Resources(200, Resources.CPU_NEGLIGIBLE)

    def __call__(self):
        try:
            return self.run()
        except:
            log.exception('Task %s failed:', self.task)
            raise
