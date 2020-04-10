from typing import List, Tuple
import numpy as np
from utils import nnet_utils, misc_utils, data_utils
from environments.environment_abstract import Environment, State
from search_methods.gbfs import GBFS
from torch.multiprocessing import Queue, get_context
import math
import time


def gbfs_runner(num_states: int, data_files: List[str], update_batch_size: int, heur_fn_i_q, heur_fn_o_q,
                proc_id: int, env: Environment, result_queue: Queue, num_steps: int,
                eps_max: float):
    heuristic_fn = nnet_utils.heuristic_fn_queue(heur_fn_i_q, heur_fn_o_q, proc_id, env)

    states: List[State]
    states, _ = data_utils.load_states_from_files(num_states, data_files)

    start_idx: int = 0
    while start_idx < num_states:
        end_idx: int = min(start_idx + update_batch_size, num_states)

        states_itr = states[start_idx:end_idx]
        eps: List[float] = list(np.random.rand(len(states_itr)) * eps_max)

        gbfs = GBFS(states_itr, env, eps=eps)
        for _ in range(num_steps):
            gbfs.step(heuristic_fn)

        trajs: List[List[Tuple[State, float]]] = gbfs.get_trajs()

        trajs_flat: List[Tuple[State, float]]
        trajs_flat, _ = misc_utils.flatten(trajs)

        is_solved: np.ndarray = np.array(gbfs.get_is_solved())

        states_update: List = []
        cost_to_go_update_l: List[float] = []
        for traj in trajs_flat:
            states_update.append(traj[0])
            cost_to_go_update_l.append(traj[1])

        cost_to_go_update = np.array(cost_to_go_update_l)

        states_update_nnet: List[np.ndaray] = env.state_to_nnet_input(states_update)

        result_queue.put((states_update_nnet, cost_to_go_update, is_solved))

        start_idx: int = end_idx

    result_queue.put(None)


class GBFSUpdater:
    def __init__(self, env: Environment, num_states: int, data_files: List[str], heur_fn_i_q, heur_fn_o_qs,
                 num_steps: int, update_batch_size: int = 1000, eps_max: float = 0.0):
        super().__init__()
        ctx = get_context("spawn")
        self.num_steps = num_steps
        num_procs = len(heur_fn_o_qs)

        # initialize queues
        self.result_queue: ctx.Queue = ctx.Queue()

        # num states per process
        num_states_per_proc: List[int] = [math.floor(num_states/num_procs) for _ in range(num_procs)]
        num_states_per_proc[-1] += num_states % num_procs

        self.num_batches: int = int(np.ceil(np.array(num_states_per_proc)/update_batch_size).sum())

        # initialize processes
        self.procs: List[ctx.Process] = []
        for proc_id in range(len(heur_fn_o_qs)):
            num_states_proc: int = num_states_per_proc[proc_id]
            if num_states_proc == 0:
                continue

            proc = ctx.Process(target=gbfs_runner, args=(num_states_proc, data_files, update_batch_size,
                                                         heur_fn_i_q, heur_fn_o_qs[proc_id], proc_id, env,
                                                         self.result_queue, num_steps, eps_max))
            proc.daemon = True
            proc.start()
            self.procs.append(proc)

    def update(self):
        states_update_nnet: List[np.ndarray]
        cost_to_go_update: np.ndarray
        is_solved: np.ndarray
        states_update_nnet, cost_to_go_update, is_solved = self._update()

        output_update = np.expand_dims(cost_to_go_update, 1)

        return states_update_nnet, output_update, is_solved

    def _update(self) -> Tuple[List[np.ndarray], np.ndarray, np.ndarray]:
        # process results
        states_update_nnet_l: List[List[np.ndarray]] = []
        cost_to_go_update_l: List = []
        is_solved_l: List = []

        none_count: int = 0
        result_count: int = 0
        display_counts: List[int] = list(np.linspace(1, self.num_batches, 10, dtype=np.int))

        start_time = time.time()

        while none_count < len(self.procs):
            result = self.result_queue.get()
            if result is None:
                none_count += 1
                continue
            result_count += 1

            states_nnet_q: List[np.ndarray]
            states_nnet_q, cost_to_go_q, is_solved_q = result
            states_update_nnet_l.append(states_nnet_q)

            cost_to_go_update_l.append(cost_to_go_q)
            is_solved_l.append(is_solved_q)

            if result_count in display_counts:
                print("%.2f%% (Total time: %.2f)" % (100 * result_count/self.num_batches, time.time() - start_time))

        num_states_nnet_np: int = len(states_update_nnet_l[0])
        states_update_nnet: List[np.ndarray] = []
        for np_idx in range(num_states_nnet_np):
            states_nnet_idx: np.ndarray = np.concatenate([x[np_idx] for x in states_update_nnet_l], axis=0)
            states_update_nnet.append(states_nnet_idx)

        cost_to_go_update: np.ndarray = np.concatenate(cost_to_go_update_l, axis=0)
        is_solved: np.ndarray = np.concatenate(is_solved_l, axis=0)

        for proc in self.procs:
            proc.join()

        return states_update_nnet, cost_to_go_update, is_solved
