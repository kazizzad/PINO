import os
import yaml
import random
from argparse import ArgumentParser
from tqdm import tqdm

import torch

from torch.optim import Adam
from torch.utils.data import DataLoader

from models import FNO3d

from train_utils.losses import LpLoss, PINO_loss3d, get_forcing
from train_utils.datasets import KFDataset, sample_data
from train_utils.utils import save_ckpt, count_params, dict2str

try:
    import wandb
except ImportError:
    wandb = None
'''
This training doesn't have extra ICs. 
'''


@torch.no_grad()
def eval_ns(model, val_loader, criterion, device):
    model.eval()
    val_err = 0.0
    for u, a in val_loader:
        u, a = u.to(device), a.to(device)
        # print(u.shape)
        # print(a.shape)
        out = model(a)
        # print(out.shape)
        val_loss = criterion(out, u)
        val_err += val_loss.item()
    avg_err = val_err / len(val_loader)
    return avg_err


def train_ns(model, 
             train_u_loader,        # training data
             val_loader,            # validation data
             optimizer, 
             scheduler,
             device, config, args):

    v = 1/ config['data']['Re']
    t_duration = config['data']['t_duration']
    num_pad = config['model']['num_pad']
    save_step = config['train']['save_step']
    eval_step = config['train']['eval_step']

    ic_weight = config['train']['ic_loss']
    f_weight = config['train']['f_loss']
    xy_weight = config['train']['xy_loss']

    raw_res = config['data']['raw_res']
    pde_res = config['data']['pde_res']
    data_res = config['data']['data_res']

    s_step = pde_res[0] // data_res[0]
    t_step = (pde_res[2] - 1) // (data_res[2] - 1)
    # set up directory
    base_dir = os.path.join('exp', config['log']['logdir'])
    ckpt_dir = os.path.join(base_dir, 'ckpts')
    os.makedirs(ckpt_dir, exist_ok=True)

    # loss fn
    lploss = LpLoss(size_average=True)
    
    S = config['data']['pde_res'][0]
    forcing = get_forcing(S).to(device)
    # set up wandb
    if wandb and args.log:
        run = wandb.init(project=config['log']['project'], 
                         entity=config['log']['entity'], 
                         group=config['log']['group'], 
                         config=config, reinit=True, 
                         settings=wandb.Settings(start_method='fork'))
    pbar = range(config['train']['num_iter'])
    pbar = tqdm(pbar, dynamic_ncols=True, smoothing=0.2)

    u_loader = sample_data(train_u_loader)

    for e in pbar:
        log_dict = {}

        optimizer.zero_grad()
        # data loss
        u, a = next(u_loader)
        u = u.to(device)
        a = a.to(device)
        # print(u.shape)
        # print(a.shape)
        out = model(a)
        # print(out.shape)
        u_pred = out[:, ::s_step, ::s_step, ::t_step]
        # print(u_pred.shape)
        data_loss = lploss(u_pred, u)
        
        if f_weight != 0.0:
            # pde loss
            u0  = a[:, :, :, 0, -1]
            loss_ic, loss_f = PINO_loss3d(out, u0, forcing, v, t_duration)
            log_dict['IC'] = loss_ic.item()
            log_dict['PDE'] = loss_f.item()
        else:
            loss_ic = loss_f = 0.0
        loss = data_loss * xy_weight + loss_f * f_weight + loss_ic * ic_weight

        loss.backward()
        optimizer.step()
        scheduler.step()

        log_dict['train loss'] = loss.item()
        log_dict['data'] = data_loss.item()
        if e % eval_step == 0:
            eval_err = eval_ns(model, val_loader, lploss, device)
            log_dict['val error'] = eval_err
        
        logstr = dict2str(log_dict)
        pbar.set_description(
            (
                logstr
            )
        )
        if wandb and args.log:
            wandb.log(log_dict)
        if e % save_step == 0:
            ckpt_path = os.path.join(ckpt_dir, f'model-{e}.pt')
            save_ckpt(ckpt_path, model, optimizer)

    # clean up wandb
    if wandb and args.log:
        run.finish()


def subprocess(args):
    with open(args.config, 'r') as f:
        config = yaml.load(f, yaml.FullLoader)
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    # set random seed
    config['seed'] = args.seed
    seed = args.seed
    torch.manual_seed(seed)
    random.seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    # create model 
    model = FNO3d(modes1=config['model']['modes1'],
                  modes2=config['model']['modes2'],
                  modes3=config['model']['modes3'],
                  fc_dim=config['model']['fc_dim'],
                  layers=config['model']['layers'], 
                  act=config['model']['act'], 
                  pad_ratio=config['model']['pad_ratio']).to(device)
    num_params = count_params(model)
    config['num_params'] = num_params
    print(f'Number of parameters: {num_params}')
    # Load from checkpoint
    if args.ckpt:
        ckpt_path = args.ckpt
        ckpt = torch.load(ckpt_path)
        model.load_state_dict(ckpt['model'])
        print('Weights loaded from %s' % ckpt_path)
    
    if args.test:
        batchsize = config['test']['batchsize']
        testset = KFDataset(paths=config['data']['paths'], 
                            raw_res=config['data']['raw_res'],
                            data_res=config['test']['data_res'], 
                            pde_res=config['test']['data_res'], 
                            n_samples=config['data']['n_test_samples'], 
                            offset=config['data']['testoffset'], 
                            t_duration=config['data']['t_duration'])
        testloader = DataLoader(testset, batch_size=batchsize, num_workers=4)
        criterion = LpLoss()
        test_err = eval_ns(model, testloader, criterion, device)
        print(f'Averaged test relative L2 error: {test_err}')
    else:
        # training set
        batchsize = config['train']['batchsize']
        u_set = KFDataset(paths=config['data']['paths'], 
                          raw_res=config['data']['raw_res'],
                          data_res=config['data']['data_res'], 
                          pde_res=config['data']['pde_res'], 
                          n_samples=config['data']['n_data_samples'], 
                          offset=config['data']['offset'], 
                          t_duration=config['data']['t_duration'])
        u_loader = DataLoader(u_set, batch_size=batchsize, num_workers=4, shuffle=True)

        # val set
        valset = KFDataset(paths=config['data']['paths'], 
                           raw_res=config['data']['raw_res'],
                           data_res=config['test']['data_res'], 
                           pde_res=config['test']['data_res'], 
                           n_samples=config['data']['n_test_samples'], 
                           offset=config['data']['testoffset'], 
                           t_duration=config['data']['t_duration'])
        val_loader = DataLoader(valset, batch_size=batchsize, num_workers=4)
        print(f'Train set: {len(u_set)}; Test set: {len(valset)};')
        optimizer = Adam(model.parameters(), lr=config['train']['base_lr'])
        scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, 
                                                         milestones=config['train']['milestones'], 
                                                         gamma=config['train']['scheduler_gamma'])
        train_ns(model, 
                 u_loader, 
                 val_loader, 
                 optimizer, scheduler, 
                 device, 
                 config, args)
    print('Done!')
        
        

if __name__ == '__main__':
    torch.backends.cudnn.benchmark = True
    # parse options
    parser = ArgumentParser(description='Basic paser')
    parser.add_argument('--config', type=str, help='Path to the configuration file')
    parser.add_argument('--log', action='store_true', help='Turn on the wandb')
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--ckpt', type=str, default=None)
    parser.add_argument('--test', action='store_true', help='Test')
    args = parser.parse_args()
    if args.seed is None:
        args.seed = random.randint(0, 100000)
    subprocess(args)