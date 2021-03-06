import argparse
import pickle
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset
import numpy as np
import pathos
import cirq
from tqdm import tqdm

from qiskit.providers.aer import AerSimulator
from qiskit.providers.aer.noise import NoiseModel
import qiskit.providers.aer.noise as noise

from my_envs import QCircuitEnv, IBMQEnv, stable_softmax


class SurrogateDataset(Dataset):

    def __init__(self, data_path):
        with open(data_path, 'rb') as f:
            data_dict = pickle.load(f)
        self.probabilities = data_dict['probability']
        self.observables = data_dict['observable']
        self.ground_truth = data_dict['meas_result']

    def __getitem__(self, idx):
        prs, obs, gts = self.probabilities[idx], self.observables[idx], self.ground_truth[idx]
        obs = np.array([[1.0, 0.0], [0.0, -1.0]])
        return torch.FloatTensor(prs), torch.tensor(obs, dtype=torch.cfloat), torch.FloatTensor([gts])

    def __len__(self):
        return len(self.probabilities)


class SurrogateGenerator:

    def __init__(self, env_path, batch_size, itrs=50):
        self.env = IBMQEnv.load(env_path)
        self.num_miti_gates = self.env.count_mitigate_gates()
        self.batch_size = batch_size
        self.itrs = itrs
        self.cur_itr = 0

    def __iter__(self):
        return self
    
    def __next__(self):
        if self.cur_itr < self.itrs:
            rand_val = torch.rand((self.batch_size, self.num_miti_gates, 4))
            prs = F.softmax(rand_val, dim=-1)
            rand_matrix = torch.randn((self.batch_size, 2, 2), dtype=torch.cfloat)
            obs = torch.bmm(rand_matrix.conj().transpose(-2, -1), rand_matrix)
            meas = self.env.step(obs.numpy(), prs.numpy(), nums=2000)
            self.cur_itr += 1
            return prs, obs, torch.FloatTensor(meas)
        else:
            self.cur_itr = 0
            raise StopIteration


class MitigateDataset(Dataset):

    def __init__(self, data_path):
        with open(data_path, 'rb') as f:
            self.dataset = pickle.load(f)

    def __getitem__(self, idx):
        obs, exp_noisy, exp_ideal = self.dataset[idx]
        return torch.tensor(obs, dtype=torch.cfloat), torch.FloatTensor([exp_noisy]), torch.FloatTensor([exp_ideal])

    def __len__(self):
        return len(self.dataset)

    
def gen_mitigation_data(args):
    env = QCircuitEnv.load(args.env_path)
    output_state = cirq.Simulator().simulate(env.circuit).final_state_vector.reshape(-1, 1)
    noisy_circuit = env.circuit.with_noise(cirq.depolarize(p=0.01))
    rho = cirq.DensityMatrixSimulator().simulate(noisy_circuit).final_density_matrix
    num_qubits = len(env.qubits)

    dataset = []
    if args.num_data < 1000:
        dataset = gen_fn(num_qubits, output_state, rho, args.num_data)
    else:
        pool = pathos.multiprocessing.Pool(processes=8)
        data_queue = []
        for i in range(args.num_data // 1000):
            data_queue.append(pool.apply_async(gen_fn, args=(num_qubits, output_state, rho, 1000)))
        pool.close()
        pool.join()
        for item in data_queue:
            dataset += item.get()

    with open(args.out_path, 'wb') as f:
        pickle.dump(dataset, f)
    print(f'Generation finished. File saved to {args.out_path}')


def gen_fn(num_qubits, output_state, rho, num_samples):
    data_list = []
    for i in range(num_samples):
        rand_matrix = torch.randn((2, 2), dtype=torch.cfloat).numpy()
        rand_obs = rand_matrix.conj().T @ rand_matrix
        obs_all = np.kron(rand_obs, np.eye((num_qubits - 1) ** 2))
        exp_ideal = output_state.conj().T @ obs_all @ output_state
        exp_ideal = round(exp_ideal.real[0][0], 8)
        exp_noisy = round(np.trace(obs_all @ rho).real, 8)
        data_list.append([rand_obs, exp_noisy, exp_ideal])

    return data_list


def gen_mitigation_data_ibmq(args):
    env = IBMQEnv.load(args.env_path)
    # env.gen_new_circuit_without_id()
    # noise_model = NoiseModel()
    # error_1 = noise.depolarizing_error(0.01, 1)  # single qubit gates
    # error_2 = noise.depolarizing_error(0.01, 2)
    # noise_model.add_all_qubit_quantum_error(error_1, ['u1', 'u2', 'u3', 'rx', 'ry', 'rz', 'i', 'x', 'y', 'z', 'h', 's', 't', 'sdg', 'tdg'])
    # noise_model.add_all_qubit_quantum_error(error_2, ['cx', 'cy', 'cz', 'ch', 'crz', 'swap', 'cu1', 'cu3', 'rzz'])

    # env.backend = AerSimulator(noise_model=noise_model)

    # print(env.circuit)
    ideal_state = env.simulate_ideal()
    noisy_state = env.simulate_noisy()
    dataset = []
    for i in tqdm(range(args.num_data)):
        # rand_matrix = torch.randn((2, 2), dtype=torch.cfloat).numpy()
        # rand_obs = rand_matrix.conj().T @ rand_matrix
        rand_matrix = (torch.rand((2, 2), dtype=torch.cfloat) * 2 - 1).numpy()
        rand_obs = (rand_matrix.conj().T + rand_matrix) / 2
        eigen_val = np.linalg.eigvalsh(rand_obs)
        rand_obs = rand_obs / np.max(np.abs(eigen_val))
        assert (rand_obs < 100).all(), eigen_val
        # rand_obs = np.diag([1., -1])
        obs = np.kron(np.eye(2**4), rand_obs)
        exp_ideal = ideal_state.expectation_value(obs).real  # (ideal_state.conj() @ np.kron(np.eye(2), rand_obs) @ ideal_state).real
        exp_noisy = noisy_state.expectation_value(obs).real
        dataset.append([rand_obs, round(exp_noisy, 8), round(exp_ideal, 8)])

    with open(args.out_path, 'wb') as f:
        pickle.dump(dataset, f)
    print(f'Generation finished. File saved to {args.out_path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-path', default='../environments/swaptest.pkl', type=str)
    parser.add_argument('--out-path', default='../data_mitigate/swaptest.pkl', type=str)
    parser.add_argument('--num-data', default=400_000, type=int)
    args = parser.parse_args()
    # dataset = SurrogateDataset('../data_surrogate/env1_data.pkl')
    # print(next(iter(dataset)))
    # dataset = SurrogateGenerator(args.env_path, batch_size=16, itrs=10)
    # for data in dataset:
    #     print(data)
    gen_mitigation_data_ibmq(args)