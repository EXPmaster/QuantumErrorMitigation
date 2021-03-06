import os
import pickle
import argparse
import torch
import torch.optim as optim
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter

from model import *
from utils import AverageMeter, build_dataloader, abs_deviation
from datasets import MitigateDataset


def main(args):
    trainset, testset, train_loader, test_loader = build_dataloader(args, MitigateDataset)
    loss_fn = nn.BCELoss()
    model_s = SurrogateModel(dim_in=4 * args.num_mitigates + 8).to(args.device)
    model_s.load_state_dict(torch.load(args.weight_path, map_location=args.device))
    model_g = Generator(dim_out=args.num_mitigates).to(args.device)
    model_d = Discriminator().to(args.device)
    optimizer_g = optim.Adam([{'params': model_g.parameters()},
                            {'params': model_s.parameters(), 'lr': 1e-6}], lr=args.lr)
    optimizer_d = optim.Adam(model_d.parameters(), lr=args.lr)
    print('Start training...')

    best_metric = 1.0
    for epoch in range(args.epochs):
        print(f'=> Epoch {epoch}')
        train(epoch, args, train_loader, model_g, model_s, model_d, loss_fn, optimizer_g, optimizer_d)
        metric = validate(epoch, args, test_loader, model_g, model_s, loss_fn)
        if metric < best_metric:
            print('Saving model...')
            best_metric = metric
            ckpt = {
                'model_g': model_g.state_dict(),
                'model_s': model_s.state_dict(),
                'model_d': model_d.state_dict(),
                'optimizer_g': optimizer_g.state_dict(),
                'optimizer_d': optimizer_d.state_dict()
            }
            torch.save(ckpt, os.path.join(args.logdir, args.save_name))
    with open(os.path.join(args.logdir, 'metric_gan.txt'), 'a+') as f:
        f.write('{} {:.6f}\n'.format(len(trainset), best_metric))


def train(epoch, args, loader, model_g, model_s, model_d, loss_fn, optimizer_g, optimizer_d):
    model_g.train()
    model_s.train()
    model_d.train()
    for itr, (obs, exp_noisy, exp_ideal) in enumerate(loader):
        # Update D to maximize log(D(x)) + log(1 - D(G(z)))
        ## real
        obs, exp_noisy, exp_ideal = obs.to(args.device), exp_noisy.to(args.device), exp_ideal.to(args.device)
        optimizer_d.zero_grad()
        labels = torch.full((args.batch_size, 1), 1.0, dtype=torch.float, device=args.device)
        output = model_d(exp_ideal, obs)
        D_ideal = output.mean().item()
        lossD_real = loss_fn(output, labels)
        lossD_real.backward()

        ## fake
        # rand_matrix = torch.randn((args.batch_size, 2, 2), dtype=torch.cfloat).to(args.device)
        # rand_hermitian = torch.bmm(rand_matrix.conj().mT, rand_matrix)
        rand_matrix = (torch.rand((args.batch_size, 2, 2), dtype=torch.cfloat) * 2.0 - 1.0).to(args.device)
        rand_hermitian = (rand_matrix.conj().mT + rand_matrix) / 2
        eigen_vals = torch.linalg.eigvalsh(rand_hermitian)
        rand_obs = rand_hermitian / eigen_vals.abs().max(1, keepdim=True)[0][:, :, None]
        rand_obs = torch.cat((rand_obs[:args.batch_size//2], obs[:args.batch_size//2]), 0)
        labels.fill_(0.0)
        fake = model_s(model_g(rand_obs), rand_obs)
        output = model_d(fake.detach(), rand_obs)
        D_g_z1 = output.mean().item()
        lossD_fake1 = loss_fn(output, labels)
        lossD_fake1.backward()

        output = model_d(exp_noisy, obs)
        D_noisy = output.mean().item()
        lossD_fake2 = loss_fn(output, labels)
        lossD_fake2.backward()
        optimizer_d.step()
        lossD = lossD_real + lossD_fake1 + lossD_fake2

        # Update G to maximize log(D(G(z)))
        optimizer_g.zero_grad()
        labels.fill_(1.0)
        output = model_d(fake, rand_obs)
        D_g_z2 = output.mean().item()
        lossG = loss_fn(output, labels)
        lossG.backward()
        optimizer_g.step()

        if itr % 1000 == 0:
            # args.writer.add_scalar('Loss/train', loss_accumulator.getval(), epoch)
            print('Loss_D: {:.4f}\tLoss_G\t{:.4f}\tD(noisy): {:.4f}\tD(ideal): {:.4f}\tD(G(z)): {:.4f} / {:.4f}'.format(lossD, lossG, D_noisy, D_ideal, D_g_z1, D_g_z2))


@torch.no_grad()
def validate(epoch, args, loader, model_g, model_s, loss_fn):
    model_g.eval()
    model_s.eval()
    metric = AverageMeter()
    for itr, (obs, exp_noisy, gts) in enumerate(loader):
        obs, exp_noisy, gts = obs.to(args.device), exp_noisy.to(args.device), gts.to(args.device)
        prs = model_g(obs)
        predicts = model_s(prs, obs)
        metric.update(abs_deviation(predicts, gts))

    value = metric.getval()
    # args.writer.add_scalar('Loss/val', losses.getval(), epoch)
    # args.writer.add_scalar('Abs_deviation/val', value, epoch)
    print('validation absolute deviation: {:.6f}'.format(value))
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train-path', default='../data_mitigate/trainset_swaptest.pkl', type=str)
    parser.add_argument('--test-path', default='../data_mitigate/testset_swaptest.pkl', type=str)
    parser.add_argument('--weight-path', default='../runs/env_swaptest/model_surrogate0.pt', type=str)
    parser.add_argument('--logdir', default='../runs/env_swaptest', type=str, help='path to save logs and models')
    parser.add_argument('--model-type', default='SurrogateModel', type=str, help='what model to use: [SurrogateModel]')
    parser.add_argument('--batch-size', default=128, type=int)
    parser.add_argument('--num-mitigates', default=5, type=int, help='number of mitigation gates')
    parser.add_argument('--workers', default=8, type=int, help='dataloader worker nums')
    parser.add_argument('--epochs', default=200, type=int)
    parser.add_argument('--gpus', default='0', type=str)
    parser.add_argument('--lr', default=1e-3, type=float, help='learning rate')
    parser.add_argument('--nosave', default=False, action='store_true', help='not to save model')
    parser.add_argument('--save-name', default='gan_model.pt', type=str, help='model file name')
    args = parser.parse_args()

    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus
    args.device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    # args.writer = SummaryWriter(log_dir=args.logdir)
    main(args)
    # args.writer.flush()
    # args.writer.close()
