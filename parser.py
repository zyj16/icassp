'''
Created on Oct 10, 2018
Tensorflow Implementation of Neural Graph Collaborative Filtering (NGCF) model in:
Wang Xiang et al. Neural Graph Collaborative Filtering. In SIGIR 2019.

@author: Xiang Wang (xiangwang@u.nus.edu)
'''
import argparse

def parse_args():
    parser = argparse.ArgumentParser(description="Run MBSSL.")

    # ******************************   optimizer paras      ***************************** #
    parser.add_argument('--lr', type=float, default=0.001,   #common parameter
                        help='Learning rate.')
    parser.add_argument('--lr_decay', action="store_true")  # default false --lr_decay
    parser.add_argument('--lr_decay_step', type=int, default=10)
    parser.add_argument('--lr_gamma', type=float, default=0.8)
    parser.add_argument('--meta_r', type=float, default=0.7)
    parser.add_argument('--meta_b', type=float, default=0.9)

    parser.add_argument('--test_epoch', type=int, default=5,
                        help='test epoch steps.')
    parser.add_argument('--weights_path', nargs='?', default='',
                        help='Store model path.')
    parser.add_argument('--data_path', nargs='?', default='./dataset/',
                        help='Input data path.')
    parser.add_argument('--proj_path', nargs='?', default='',
                        help='Project path.')

    parser.add_argument('--dataset', nargs='?', default='Tmall',
                        help='Choose a dataset from {Beibei,Taobao}')
    parser.add_argument('--verbose', type=int, default=1,
                        help='Interval of evaluation.')
    parser.add_argument('--is_norm', type=int, default=1,
                    help='Interval of evaluation.')
    parser.add_argument('--epoch', type=int, default=500,
                        help='Number of epoch.')

    parser.add_argument('--embed_size', type=int, default=64,   #common parameter  temperature
                        help='Embedding size.')
    parser.add_argument('--layer_size', nargs='?', default='[64,64,64,64]', #common parameter
                        help='Output sizes of every layer')
    parser.add_argument('--batch_size', type=int, default=256,
                        help='Batch size.')
    parser.add_argument('--nhead', type=int, default=1)
    parser.add_argument('--n_intent', type=int, default=4)# 4 best
    parser.add_argument('--regs', nargs='?', default='[1e-5,1e-5,1e-5,1e-2]',
                        help='Regularizations.')

    parser.add_argument('--adj_type', nargs='?', default='pre',
                        help='Specify the type of the adjacency (laplacian) matrix from {plain, norm, mean}.')
    parser.add_argument('--gpu_id', type=int, default=0,
                        help='Gpu id')

    parser.add_argument('--node_dropout_flag', type=int, default=1,
                        help='0: Disable node dropout, 1: Activate node dropout')
    parser.add_argument('--node_dropout', nargs='?', default='[0.1]',
                        help='Keep probability w.r.t. node dropout (i.e., 1-dropout_ratio) for each deep layer. 1: no dropout.')

    parser.add_argument('--Ks', nargs='?', default='[10,20,40,80]',
                        help='K for Top-K list')

    parser.add_argument('--save_flag', type=int, default=0,
                        help='0: Disable model saver, 1: Activate model saver')

    parser.add_argument('--test_flag', nargs='?', default='part',
                        help='Specify the test type from {part, full}, indicating whether the reference is done in mini-batch')

    parser.add_argument('--report', type=int, default=0,
                        help='0: Disable performance report w.r.t. sparsity levels, 1: Show performance report w.r.t. sparsity levels')


    parser.add_argument('--aux_beh_idx', nargs='?', default='[0,1,2]') #+1
    parser.add_argument('--all_beh_idx', nargs='?', default='[0,1,2,3]')#+1

    # ******************************   ssl inner loss  paras      ***************************** #
    parser.add_argument('--aug_type', type=int, default=0)
    parser.add_argument('--temperature', type=float, default=0.5)
    parser.add_argument('--ssl_ratio', type=float, default=0.5)
    parser.add_argument('--ssl_temp', type=float, default=0.5)
    parser.add_argument('--ssl_reg', type=float, default=1)
    parser.add_argument('--ssl_mode', type=str, default='both_side')

    # ******************************   ssl inter loss  paras      ***************************** #
    parser.add_argument('--ssl_reg_inter', nargs='?', default='[1,1,1,1]')#+1
    parser.add_argument('--ssl_inter_mode', type=str, default='both_side')

    # ******************************  ssl2 similarity paras      ***************************** #
    parser.add_argument('--sim_measure', type=str, default='swing')
    parser.add_argument('--topk1_user', type=int, default=10)  #
    parser.add_argument('--topk1_item', type=int, default=10)  #

    # ******************************   model hyper paras      ***************************** #
    parser.add_argument('--n_fold', type=int, default=50)
    parser.add_argument('--att_dim', type=int, default=8, help='self att dim')  # d_a
    parser.add_argument('--wid', nargs='?', default='[0.01,0.01,0.01,0.01]',
                        help='negative weight, [0.1,0.1,0.1] for beibei, [0.01,0.01,0.01] for taobao,[0.01,0.01,0.01,0.01] for tmall') #+1

    parser.add_argument('--decay', type=float, default=0.01,
                        help='Regularization, 10 for beibei, 0.01 for taobao, 0.01 for tmall')  # regularization decay

    parser.add_argument('--mf_decay', type=float, default=1, help='mf loss decay')  # mf loss decay

    parser.add_argument('--coefficient', nargs='?', default='[1.0/6, 4.0/6, 4.0/6, 1.0/6] ',
                        help='Regularization, [0.0/6, 5.0/6, 1.0/6] for beibei, [1.0/6, 4.0/6, 1.0/6] for taobao, [1.0/6, 4.0/6, 4.0/6, 1.0/6] for tmall')#+1

    parser.add_argument('--mess_dropout', nargs='?', default='[0.3]',
                        help='Keep probability w.r.t. message dropout')

    parser.add_argument('--dropout_ratio', type=float, default=0.5)
    
    
    
    #****************************diffusion parameter*************************************#
    parser.add_argument('--mean_type', type=str, default='x0', help='MeanType for diffusion: x0, eps')
    parser.add_argument('--steps', type=int, default=100, help='diffusion steps')
    parser.add_argument('--noise_schedule', type=str, default='linear-var', help='the schedule for noise generating')
    parser.add_argument('--noise_scale', type=float, default=0.1, help='noise scale for noise generating')
    parser.add_argument('--noise_min', type=float, default=0.01)
    parser.add_argument('--noise_max', type=float, default=0.09)
    parser.add_argument('--sampling_noise', type=bool, default=False, help='sampling with noise or not')
    parser.add_argument('--sampling_steps', type=int, default=100, help='steps for sampling/denoising')
    parser.add_argument('--reweight', type=bool, default=True, help='assign different weight to different timestep or not')
    parser.add_argument('--hidden2', '-h2', type=int, default=8, help='Number of units in hidden layer 2.')


    # params for the MLP
    parser.add_argument('--time_type', type=str, default='cat', help='cat or add')
    parser.add_argument('--mlp_dims', type=str, default='[8]', help='the dims for the DNN')
    parser.add_argument('--norm', type=bool, default=False, help='Normalize the input or not')
    parser.add_argument('--emb_size', type=int, default=10, help='timestep embedding size')
    parser.add_argument('--mlp_act_func', type=str, default='tanh', help='the activation function for MLP')
    parser.add_argument('--optimizer2', type=str, default='AdamW',
                    help='optimizer for MLP: Adam, AdamW, SGD, Adagrad, Momentum')


    
    return parser.parse_args()
