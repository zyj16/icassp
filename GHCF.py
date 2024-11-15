# -*- coding: utf-8 -*-
# @Time : 2023/1/3 20:33
# @Author : crisis
# @FileName:
# @Function:
# @Usage:
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import sys
import copy
from utility.helper import *
from utility.batch_test import *
import multiprocessing
import torch.multiprocessing
import random
from utility.optimize import HMG
import math

class Augmentor():
    def __init__(self, data_config, args):
        self.n_users = data_config['n_users']
        self.n_items = data_config['n_items']
        self.train_matrix = data_config['trn_mat']
        self.train_mats = data_config['trnMats']
        self.training_user, self.training_item = self.get_train_interactions()
        self.ssl_ratio = args.ssl_ratio
        self.aug_type = args.aug_type

    def get_train_interactions(self):
        users_list, items_list = [], []
        for i in range(len(self.train_mats)):
            train_matrix = self.train_mats[i]
            user_beha_list = []
            item_beha_list = []
            for (user, item), value in train_matrix.items():
                user_beha_list.append(user)
                item_beha_list.append(item)
            users_list.append(user_beha_list)
            items_list.append(item_beha_list)
        return users_list, items_list

    def augment_adj_mat(self, aug_type=0):
        aug_mat_list = []
        for i in range(len(self.training_user)):
            np.seterr(divide='ignore')
            n_nodes = self.n_users + self.n_items
            if aug_type in [0, 1, 2] and self.ssl_ratio > 0:
                # data augmentation type --- 0: Node Dropout; 1: Edge Dropout; 2: Random Walk
                if aug_type == 0:
                    drop_user_idx = np.random.choice(self.n_users, size=int(self.n_users * self.ssl_ratio),
                                                     replace=False)
                    drop_item_idx = np.random.choice(self.n_items, size=int(self.n_items * self.ssl_ratio),
                                                     replace=False)
                    indicator_user = np.ones(self.n_users, dtype=np.float32)
                    indicator_item = np.ones(self.n_items, dtype=np.float32)
                    indicator_user[drop_user_idx] = 0.
                    indicator_item[drop_item_idx] = 0.
                    diag_indicator_user = sp.diags(indicator_user)  # [n_user, n_user]
                    diag_indicator_item = sp.diags(indicator_item)  # [n_item, n_item]
                    R = sp.csr_matrix(
                        (np.ones_like(self.training_user[i], dtype=np.float32), (self.training_user[i], self.training_item[i])),
                        shape=(self.n_users, self.n_items))
                    R_prime = diag_indicator_user.dot(R).dot(
                        diag_indicator_item)
                    (user_np_keep, item_np_keep) = R_prime.nonzero()
                    ratings_keep = R_prime.data
                    tmp_adj = sp.csr_matrix((ratings_keep, (user_np_keep, item_np_keep + self.n_users)),
                                            shape=(n_nodes, n_nodes))
                if aug_type in [1, 2]:
                    keep_idx = np.random.choice(len(self.training_user[i]),
                                                size=int(len(self.training_user[i]) * (1 - self.ssl_ratio)),
                                                replace=False)
                    user_np = np.array(self.training_user[i])[keep_idx]
                    item_np = np.array(self.training_item[i])[keep_idx]
                    ratings = np.ones_like(user_np, dtype=np.float32)
                    tmp_adj = sp.csr_matrix((ratings, (user_np, item_np + self.n_users)), shape=(n_nodes, n_nodes))

            adj_mat = tmp_adj + tmp_adj.T

            rowsum = np.array(adj_mat.sum(1))
            d_inv = np.power(rowsum, -0.5).flatten()
            d_inv[np.isinf(d_inv)] = 0.
            d_mat_inv = sp.diags(d_inv)
            norm_adj_tmp = d_mat_inv.dot(adj_mat)
            adj_matrix = norm_adj_tmp.dot(d_mat_inv)
            aug_mat_list.append(adj_matrix.tocsr())

        return aug_mat_list#adj_matrix.tocsr()


class MBSSL(nn.Module):
    name = 'MBSSL'

    def __init__(self, max_item_list, data_config, args):
        super(MBSSL, self).__init__()
        # ********************** input data *********************** #
        self.max_item_list = max_item_list
        self.n_users = data_config['n_users']
        self.n_items = data_config['n_items']
        self.num_nodes = self.n_users + self.n_items
        self.pre_adjs = data_config['pre_adjs']
        self.pre_adjs_tensor = self._convert_sp_mat_to_sp_tensor(self.pre_adjs)
        self.behs = data_config['behs']
        self.n_relations = len(self.behs)
        # ********************** hyper parameters *********************** #
        self.coefficient = torch.tensor(eval(args.coefficient)).view(1, -1).to(device)
        self.emb_dim = args.embed_size
        self.batch_size = args.batch_size
        self.weight_size = eval(args.layer_size)
        self.n_layers = len(self.weight_size)
        self.mess_dropout = eval(args.mess_dropout)  # dropout ratio
        self.aug_type = args.aug_type
        self.nhead = args.nhead
        self.att_dim = args.att_dim
        self.n_intent = args.n_intent
        # ********************** learnable parameters *********************** #
        self.all_weights = {}
        self.all_weights['user_embedding'] = Parameter(torch.FloatTensor(self.n_users, self.emb_dim))
        self.all_weights['item_embedding'] = Parameter(torch.FloatTensor(self.n_items, self.emb_dim))
        self.all_weights['relation_embedding'] = Parameter(torch.FloatTensor(self.n_relations, self.emb_dim))
        
        self.weight_size_list = [self.emb_dim] + self.weight_size

        for k in range(self.n_layers):
            self.all_weights['W_gc_%d' % k] = Parameter(
                torch.FloatTensor(self.weight_size_list[k], self.weight_size_list[k + 1]))
            self.all_weights['W_rel_%d' % k] = Parameter(
                torch.FloatTensor(self.weight_size_list[k], self.weight_size_list[k + 1]))

        self.all_weights['trans_weights_s1'] = Parameter(
            torch.FloatTensor(self.n_relations, self.emb_dim, self.att_dim))
        self.all_weights['trans_weights_s2'] = Parameter(torch.FloatTensor(self.n_relations, self.att_dim, 1))
        # self.all_weights['trans_weights_s3'] = Parameter(torch.FloatTensor(self.emb_dim,self.emb_dim))
        # self.layer_norm_5 = nn.LayerNorm(self.emb_dim)
        self.reset_parameters()
        self.all_weights = nn.ParameterDict(self.all_weights)
        self.dropout = nn.Dropout(self.mess_dropout[0], inplace=True)
        self.leaky_relu = nn.LeakyReLU(inplace=True)
        # self.intentattnlayer = IntentAttention(data_config,args)
    def reset_parameters(self):
        nn.init.xavier_uniform_(self.all_weights['user_embedding'])
        nn.init.xavier_uniform_(self.all_weights['item_embedding'])
        nn.init.xavier_uniform_(self.all_weights['relation_embedding'])
        nn.init.xavier_uniform_(self.all_weights['trans_weights_s1'])
        nn.init.xavier_uniform_(self.all_weights['trans_weights_s2'])
        for k in range(self.n_layers):
            nn.init.xavier_uniform_(self.all_weights['W_gc_%d' % k])
            nn.init.xavier_uniform_(self.all_weights['W_rel_%d' % k])

    def _convert_sp_mat_to_sp_tensor(self, X_list):
        sparse_tensor_list = []
        for i in range(len(X_list)):
            X=X_list[i]
            coo = X.tocoo()
            values = coo.data
            indices = np.vstack((coo.row, coo.col))
            shape = coo.shape
            sparse_tensor_list.append((torch.sparse.FloatTensor(torch.LongTensor(indices), torch.FloatTensor(values), torch.Size(shape))).to(device))

        return sparse_tensor_list #torch.sparse.FloatTensor(torch.LongTensor(indices), torch.FloatTensor(values), torch.Size(shape))

    def forward(self, sub_mats, device):
        self.sub_mat = {}
        for k in range(1, self.n_layers + 1):
            if self.aug_type in [0, 1]:
                self.sub_mat['sub_mat_1%d' % k] = sub_mats['sub1']
                self.sub_mat['sub_mat_2%d' % k] = sub_mats['sub2']
            else:
                self.sub_mat['sub_mat_1%d' % k] = sub_mats['sub1%d' % k]
                self.sub_mat['sub_mat_2%d' % k] = sub_mats['sub2%d' % k]

        ego_embeddings = torch.cat((self.all_weights['user_embedding'], self.all_weights['item_embedding']),
                                   dim=0).unsqueeze(1).repeat(1, self.n_relations, 1)
        

        all_embeddings = ego_embeddings
       

        all_rela_embs = {}
        for i in range(self.n_relations):
            beh = self.behs[i]
            rela_emb = self.all_weights['relation_embedding'][i]
            rela_emb = torch.reshape(rela_emb, (-1, self.emb_dim))
            all_rela_embs[beh] = [rela_emb]

        total_mm_time = 0.
        for k in range(0, self.n_layers):
            embeddings_list = []
            for i in range(self.n_relations):
                st = time()
                embeddings_ = torch.matmul(self.pre_adjs_tensor[i], ego_embeddings[:, i, :])
                total_mm_time += time() - st
                rela_emb = all_rela_embs[self.behs[i]][k]
                embeddings_ = self.leaky_relu(
                    torch.matmul(torch.mul(embeddings_, rela_emb), self.all_weights['W_gc_%d' % k]))
                embeddings_list.append(embeddings_)
            embeddings_st = torch.stack(embeddings_list, dim=1)
            ego_embeddings = self.dropout(embeddings_st)
            all_embeddings = all_embeddings + ego_embeddings

            for i in range(self.n_relations):
                rela_emb = torch.matmul(all_rela_embs[self.behs[i]][k],
                                        self.all_weights['W_rel_%d' % k])
                all_rela_embs[self.behs[i]].append(rela_emb)

        all_embeddings /= self.n_layers + 1
        u_g_embeddings, i_g_embeddings = torch.split(all_embeddings, [self.n_users, self.n_items], 0)
        token_embedding = torch.zeros([1, self.n_relations, self.emb_dim], device=device)
        i_g_embeddings = torch.cat((i_g_embeddings, token_embedding), dim=0)


        attn_user, attn_item = 0,0 #torch.split(attn, [self.n_users, self.n_items], 0)

        for i in range(self.n_relations):
            all_rela_embs[self.behs[i]] = torch.mean(torch.stack(all_rela_embs[self.behs[i]], 0), 0)

        return u_g_embeddings,i_g_embeddings,all_rela_embs, attn_user, attn_item

class IntentAttention(nn.Module):
    def __init__(self, data_config, args):
        super(IntentAttention, self).__init__()
        self.emb_dim = args.embed_size
        self.behs = data_config['behs']
        self.n_relations = len(self.behs)
        self.layer_norm_1 = nn.LayerNorm(self.emb_dim)
        self.layer_norm_2 = nn.LayerNorm(self.emb_dim)
        self.layer_norm_3 = nn.LayerNorm(self.emb_dim)
        self.layer_norm_4 = nn.LayerNorm(self.emb_dim)
        self.W = nn.Linear(self.emb_dim, self.emb_dim)
        self.W1 = nn.Linear(self.emb_dim, self.emb_dim)
    def forward(self, u_intent_embedding, i_intent_embedding):
        # user side
        u_i_embeddings = []
        u_query, _ = torch.max(u_intent_embedding[:, -1, :, :], dim=1)
        user_query = self.layer_norm_1(u_query).unsqueeze(1)#num_user,1,d
        for i in range(self.n_relations-1):
            u_key= u_intent_embedding[:, i, :, :]
            u_key_hat = self.layer_norm_2(u_key)
            user_key = u_key_hat + torch.relu(self.W(u_key_hat)) # num_user, num_intent,d
            logits = user_query @ user_key.permute(0, 2, 1)  # [num_user, 1, num_intent]
            logits = logits.squeeze() / math.sqrt(self.emb_dim)
            score = F.softmax(logits, -1)
            u_i_embeddings.append(score.unsqueeze(-1)*user_key)
        u_i_embeddings.append(u_intent_embedding[:, -1, :, :])

        # item side
        i_i_embeddings = []
        i_query,_ = torch.max(i_intent_embedding[:, -1, :, :], dim=1)
        item_query = self.layer_norm_3(i_query).unsqueeze(1)  # num_user,1,d
        for i in range(self.n_relations-1):
            i_key = i_intent_embedding[:, i, :, :]
            i_key_hat = self.layer_norm_4(i_key)
            item_key = i_key_hat + torch.relu(self.W(i_key_hat))  # num_user, num_intent,d
            logits = item_query @ item_key.permute(0, 2, 1)  # [num_user, 1, num_intent]
            logits = logits.squeeze() / math.sqrt(self.emb_dim)
            score = F.softmax(logits, -1)
            i_i_embeddings.append(score.unsqueeze(-1) * item_key)
        i_i_embeddings.append(i_intent_embedding[:, -1, :, :])

        u_ini_embeddings = torch.stack(u_i_embeddings,dim=1)
        i_i_embeddings = torch.stack(i_i_embeddings,dim=1)

        return u_ini_embeddings, i_i_embeddings
class RecLoss(nn.Module):
    def __init__(self, data_config, args):
        super(RecLoss, self).__init__()
        self.behs = data_config['behs']
        self.n_relations = len(self.behs)
        self.n_users = data_config['n_users']
        self.n_items = data_config['n_items']
        self.emb_dim = args.embed_size
        self.coefficient = eval(args.coefficient)
        self.wid = eval(args.wid)

    def forward(self, input_u, label_phs, ua_embeddings, ia_embeddings, rela_embeddings):
        uid = ua_embeddings[input_u]
        uid = torch.reshape(uid, (-1, self.n_relations, self.emb_dim))
        pos_r_list = []
        for i in range(self.n_relations):
            beh = self.behs[i]
            pos_beh = ia_embeddings[:, i, :][label_phs[i]]  # [B, max_item, dim]
            pos_num_beh = torch.ne(label_phs[i], self.n_items).float()
            pos_beh = torch.einsum('ab,abc->abc', pos_num_beh,
                                   pos_beh)  # [B, max_item] * [B, max_item, dim] -> [B, max_item, dim]
            pos_r = torch.einsum('ac,abc->abc', uid[:, i, :],
                                 pos_beh)  # [B, dim] * [B, max_item, dim] -> [B, max_item, dim]
            pos_r = torch.einsum('ajk,lk->aj', pos_r, rela_embeddings[beh])
            pos_r_list.append(pos_r)

        loss = 0.
        for i in range(self.n_relations):
            beh = self.behs[i]
            temp = torch.einsum('ab,ac->bc', ia_embeddings[:, i, :], ia_embeddings[:, i, :]) \
                   * torch.einsum('ab,ac->bc', uid[:, i, :], uid[:, i, :])  # [B, dim]' * [B, dim] -> [dim, dim]
            tmp_loss = self.wid[i] * torch.sum(
                temp * torch.matmul(rela_embeddings[beh].T, rela_embeddings[beh]))
            tmp_loss += torch.sum((1.0 - self.wid[i]) * torch.square(pos_r_list[i]) - 2.0 * pos_r_list[i])

            loss += self.coefficient[i] * tmp_loss

        regularizer = torch.sum(torch.square(uid)) * 0.5 + torch.sum(torch.square(ia_embeddings)) * 0.5
        emb_loss = args.decay * regularizer

        return loss, emb_loss


# class SSLoss(nn.Module):
#     def __init__(self, data_config, args):
#         super(SSLoss, self).__init__()
#         self.ssl_temp = args.ssl_temp
#         self.ssl_reg = args.ssl_reg
#         self.ssl_mode = args.ssl_mode

#     def forward(self, input_u_list, input_i_list, ua_embeddings_sub1, ua_embeddings_sub2, ia_embeddings_sub1,
#                 ia_embeddings_sub2):
#         if self.ssl_mode in ['user_side', 'both_side']:
#             user_emb1 = ua_embeddings_sub1[input_u_list]
#             user_emb2 = ua_embeddings_sub2[input_u_list]  # [B, dim]
#             normalize_user_emb1 = F.normalize(user_emb1, dim=1)
#             normalize_user_emb2 = F.normalize(user_emb2, dim=1)
#             normalize_all_user_emb2 = F.normalize(ua_embeddings_sub2, dim=1)
#             pos_score_user = torch.sum(torch.mul(normalize_user_emb1, normalize_user_emb2),
#                                        dim=1)
#             pos_score_user = torch.exp(pos_score_user / self.ssl_temp)

#             ttl_score_user = torch.matmul(normalize_user_emb1,
#                                           normalize_all_user_emb2.T)
#             ttl_score_user = torch.sum(torch.exp(ttl_score_user / self.ssl_temp), dim=1)  # [B, ]

#             ssl_loss_user = -torch.sum(torch.log(pos_score_user / ttl_score_user))

#         if self.ssl_mode in ['item_side', 'both_side']:
#             item_emb1 = ia_embeddings_sub1[input_i_list]
#             item_emb2 = ia_embeddings_sub2[input_i_list]

#             normalize_item_emb1 = F.normalize(item_emb1, dim=1)
#             normalize_item_emb2 = F.normalize(item_emb2, dim=1)
#             normalize_all_item_emb2 = F.normalize(ia_embeddings_sub2, dim=1)
#             pos_score_item = torch.sum(torch.mul(normalize_item_emb1, normalize_item_emb2), dim=1)
#             ttl_score_item = torch.matmul(normalize_item_emb1, normalize_all_item_emb2.T)

#             pos_score_item = torch.exp(pos_score_item / self.ssl_temp)
#             ttl_score_item = torch.sum(torch.exp(ttl_score_item / self.ssl_temp), dim=1)

#             ssl_loss_item = -torch.sum(torch.log(pos_score_item / ttl_score_item))

#         if self.ssl_mode == 'user_side':
#             ssl_loss = self.ssl_reg * ssl_loss_user
#         elif self.ssl_mode == 'item_side':
#             ssl_loss = self.ssl_reg * ssl_loss_item
#         else:
#             ssl_loss = self.ssl_reg * (ssl_loss_user + ssl_loss_item)

#         return ssl_loss
class local_ssl(nn.Module):
    def __init__(self, data_config, args):
        super(local_ssl, self).__init__()
        self.config = data_config
        self.ssl_temp = args.ssl_temp
        self.ssl_reg_inter = eval(args.ssl_reg_inter)
        self.ssl_mode_inter = args.ssl_inter_mode
        self.embed_size = args.embed_size
        self.temperature = args.temperature  # temperature
        self.criterion = nn.CrossEntropyLoss()
    def forward(self, input_u_list, input_i_list, u_sub1_embeddings, i_sub1_embeddings,u_sub2_embeddings,i_sub2_embeddings):
        u_sub1_embeddings = u_sub1_embeddings.contiguous().view(-1, self.embed_size)
        u_sub2_embeddings = u_sub2_embeddings.contiguous().view(-1, self.embed_size)
        i_sub1_embeddings = i_sub1_embeddings.contiguous().view(-1, self.embed_size)
        i_sub2_embeddings = i_sub2_embeddings.contiguous().view(-1, self.embed_size)
        # user side
        aug_hidden_view1 = F.normalize(u_sub1_embeddings[input_u_list],dim=1)
        aug_hidden_view2 = F.normalize(u_sub2_embeddings[input_u_list],dim=1)

        sim11 = aug_hidden_view1 @ aug_hidden_view1.t()
        sim22 = aug_hidden_view2 @ aug_hidden_view2.t()
        sim12 = aug_hidden_view1 @ aug_hidden_view2.t()

        sim11[..., range(sim11.size(0)), range(sim11.size(0))] = float('-inf')
        sim22[..., range(sim22.size(0)), range(sim22.size(0))] = float('-inf')

        cl_logits1 = torch.cat([sim12, sim11], -1)
        cl_logits2 = torch.cat([sim22, sim12.t()], -1)
        cl_logits = torch.cat([cl_logits1, cl_logits2], 0) / self.temperature

        target = torch.arange(cl_logits.size(0)).long().to(aug_hidden_view1.device)
        user_cl_loss = self.criterion(cl_logits, target)
        # item side
        aug_hidden_view1 = i_sub1_embeddings[input_i_list]
        aug_hidden_view2 = i_sub2_embeddings[input_i_list]
        # user side
        sim11 = aug_hidden_view1 @ aug_hidden_view1.t()
        sim22 = aug_hidden_view2 @ aug_hidden_view2.t()
        sim12 = aug_hidden_view1 @ aug_hidden_view2.t()

        sim11[..., range(sim11.size(0)), range(sim11.size(0))] = float('-inf')
        sim22[..., range(sim22.size(0)), range(sim22.size(0))] = float('-inf')

        cl_logits1 = torch.cat([sim12, sim11], -1)
        cl_logits2 = torch.cat([sim22, sim12.t()], -1)
        cl_logits = torch.cat([cl_logits1, cl_logits2], 0) / self.temperature

        target = torch.arange(cl_logits.size(0)).long().to(aug_hidden_view1.device)
        item_cl_loss = self.criterion(cl_logits, target)

        cl_loss = user_cl_loss+item_cl_loss

        return cl_loss

class global_ssl(nn.Module):
    def __init__(self, data_config, args):
        super(global_ssl, self).__init__()
        self.config = data_config
        self.ssl_temp = args.ssl_temp
        self.ssl_reg_inter = eval(args.ssl_reg_inter)
        self.ssl_mode_inter = args.ssl_inter_mode
        self.embed_size = args.embed_size
        self.temperature = args.temperature  # temperature
        self.criterion = nn.CrossEntropyLoss()
    def forward(self, input_u_list, input_i_list, u_sub1_embeddings, i_sub1_embeddings,u_sub2_embeddings,i_sub2_embeddings):
        u_sub1_embeddings = u_sub1_embeddings.contiguous().view(-1, self.embed_size)
        u_sub2_embeddings = u_sub2_embeddings.contiguous().view(-1, self.embed_size)
        i_sub1_embeddings = i_sub1_embeddings.contiguous().view(-1, self.embed_size)
        i_sub2_embeddings = i_sub2_embeddings.contiguous().view(-1, self.embed_size)
        # user side
        aug_hidden_view1 = F.normalize(u_sub1_embeddings[input_u_list],dim=1)
        aug_hidden_view2 = F.normalize(u_sub2_embeddings[input_u_list],dim=1)

        sim11 = aug_hidden_view1 @ aug_hidden_view1.t()
        sim22 = aug_hidden_view2 @ aug_hidden_view2.t()
        sim12 = aug_hidden_view1 @ aug_hidden_view2.t()

        sim11[..., range(sim11.size(0)), range(sim11.size(0))] = float('-inf')
        sim22[..., range(sim22.size(0)), range(sim22.size(0))] = float('-inf')

        cl_logits1 = torch.cat([sim12, sim11], -1)
        cl_logits2 = torch.cat([sim22, sim12.t()], -1)
        cl_logits = torch.cat([cl_logits1, cl_logits2], 0) / self.temperature

        target = torch.arange(cl_logits.size(0)).long().to(aug_hidden_view1.device)
        user_cl_loss = self.criterion(cl_logits, target)
        # item side
        aug_hidden_view1 = i_sub1_embeddings[input_i_list]
        aug_hidden_view2 = i_sub2_embeddings[input_i_list]
        # user side
        sim11 = aug_hidden_view1 @ aug_hidden_view1.t()
        sim22 = aug_hidden_view2 @ aug_hidden_view2.t()
        sim12 = aug_hidden_view1 @ aug_hidden_view2.t()

        sim11[..., range(sim11.size(0)), range(sim11.size(0))] = float('-inf')
        sim22[..., range(sim22.size(0)), range(sim22.size(0))] = float('-inf')

        cl_logits1 = torch.cat([sim12, sim11], -1)
        cl_logits2 = torch.cat([sim22, sim12.t()], -1)
        cl_logits = torch.cat([cl_logits1, cl_logits2], 0) / self.temperature

        target = torch.arange(cl_logits.size(0)).long().to(aug_hidden_view1.device)
        item_cl_loss = self.criterion(cl_logits, target)

        cl_loss = user_cl_loss+item_cl_loss

        return cl_loss




def get_lables(temp_set, k=0.9999):
    max_item = 0
    item_lenth = []
    for i in temp_set:
        item_lenth.append(len(temp_set[i]))
        if len(temp_set[i]) > max_item:
            max_item = len(temp_set[i])
    item_lenth.sort()

    max_item = item_lenth[int(len(item_lenth) * k) - 1]

    print(max_item)
    for i in temp_set:
        if len(temp_set[i]) > max_item:
            temp_set[i] = temp_set[i][0:max_item]
        while len(temp_set[i]) < max_item:
            temp_set[i].append(n_items)
    return max_item, temp_set


def get_train_instances1(max_item_list, beh_label_list):
    user_train = []
    beh_item_list = [list() for i in range(n_behs)]  #

    for i in beh_label_list[-1].keys():
        user_train.append(i)
        beh_item_list[-1].append(beh_label_list[-1][i])
        for j in range(n_behs - 1):
            if not i in beh_label_list[j].keys():
                beh_item_list[j].append([n_items] * max_item_list[j])
            else:
                beh_item_list[j].append(beh_label_list[j][i])

    user_train = np.array(user_train)
    beh_item_list = [np.array(beh_item) for beh_item in beh_item_list]
    user_train = user_train[:, np.newaxis]
    return user_train, beh_item_list


def get_train_pairs(user_train_batch, beh_item_tgt_batch):
    input_u_list, input_i_list = [], []
    for i in range(len(user_train_batch)):
        pos_items = beh_item_tgt_batch[i][np.where(beh_item_tgt_batch[i] != n_items)]  # ndarray [x,]
        uid = user_train_batch[i][0]
        input_u_list += [uid] * len(pos_items)
        input_i_list += pos_items.tolist()

    return np.array(input_u_list).reshape([-1]), np.array(input_i_list).reshape([-1])


def test_torch(ua_embeddings, ia_embeddings, rela_embedding, users_to_test, batch_test_flag=False):
    def get_score_np(ua_embeddings, ia_embeddings, rela_embedding, users, items):
        ug_embeddings = ua_embeddings[users]  # []
        pos_ig_embeddings = ia_embeddings[items]
        dot = np.multiply(pos_ig_embeddings, rela_embedding)  # [I, dim] * [1, dim]-> [I, dim]
        batch_ratings = np.matmul(ug_embeddings, dot.T)  # [U, dim] * [dim, I] -> [U, I]
        return batch_ratings

    result = {'precision': np.zeros(len(Ks)), 'recall': np.zeros(len(Ks)), 'ndcg': np.zeros(len(Ks)),
              'hit_ratio': np.zeros(len(Ks)), 'auc': 0.}

    test_users = users_to_test
    n_test_users = len(test_users)

    # pool = torch.multiprocessing.Pool(cores)
    pool = multiprocessing.Pool(cores)

    u_batch_size = BATCH_SIZE
    n_user_batchs = n_test_users // u_batch_size + 1

    count = 0
    for u_batch_id in range(n_user_batchs):
        start = u_batch_id * u_batch_size
        end = (u_batch_id + 1) * u_batch_size

        user_batch = test_users[start: end]

        item_batch = range(ITEM_NUM)
        rate_batch = get_score_np(ua_embeddings, ia_embeddings, rela_embedding, user_batch, item_batch)

        user_batch_rating_uid = zip(rate_batch, user_batch)
        batch_result = pool.map(test_one_user, user_batch_rating_uid)
        count += len(batch_result)

        for re in batch_result:
            result['precision'] += re['precision'] / n_test_users
            result['recall'] += re['recall'] / n_test_users
            result['ndcg'] += re['ndcg'] / n_test_users
            result['hit_ratio'] += re['hit_ratio'] / n_test_users
            result['auc'] += re['auc'] / n_test_users
    assert count == n_test_users

    pool.close()
    return result


def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if you are using multi-GPU.
    np.random.seed(seed)  # Numpy module.
    random.seed(seed)  # Python random module.

    # torch.set_deterministic(True)
    # # torch.backends.cudnn.deterministic = True
    # torch.backends.cudnn.enabled = False
    # torch.backends.cudnn.benchmark = False
    os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'
    os.environ['PYTHONHASHSEED'] = str(seed)


if __name__ == '__main__':
    torch.autograd.set_detect_anomaly(True)

    os.environ["GIT_PYTHON_REFRESH"] = "quiet"
    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    set_seed(2020)

    config = dict()
    config['device'] = device
    config['n_users'] = data_generator.n_users
    config['n_items'] = data_generator.n_items
    config['behs'] = data_generator.behs
    config['trn_mat'] = data_generator.trnMats[-1]  # 目标行为交互矩阵
    config['trnMats'] = data_generator.trnMats
    """
    *********************************************************
    Generate the Laplacian matrix, where each entry defines the decay factor (e.g., p_ui) between two connected nodes.
    """

    pre_adj_list = data_generator.get_adj_mat()
    config['pre_adjs'] = pre_adj_list
    print('use the pre adjcency matrix')
    n_users, n_items = data_generator.n_users, data_generator.n_items
    behs = data_generator.behs
    n_behs = data_generator.beh_num

    # user_sim_mat_unified, item_sim_mat_unified = data_generator.get_unified_sim(args.sim_measure)
    #
    # config['user_sim'] = user_sim_mat_unified.todense()
    # config['item_sim'] = item_sim_mat_unified.todense()

    # user_indices, item_indices = preprocess_sim(args, config)

    trnDicts = copy.deepcopy(data_generator.trnDicts)
    max_item_list = []
    beh_label_list = []
    for i in range(n_behs):
        max_item, beh_label = get_lables(trnDicts[i])
        max_item_list.append(max_item)
        beh_label_list.append(beh_label)

    t0 = time()

    model = MBSSL(max_item_list, data_config=config, args=args).to(device)
    augmentor = Augmentor(data_config=config, args=args)
    recloss = RecLoss(data_config=config, args=args).to(device)
    # ssloss = SSLoss(data_config=config, args=args).to(device)
    # ssloss2 = SSLoss2(data_config=config, args=args).to(device)
    align_ssl_loss = local_ssl(data_config=config, args=args).to(device)
    diff_ssl_loss = global_ssl(data_config=config, args=args).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    hmg = HMG(model.parameters(), relax_factor=args.meta_r, beta=args.meta_b)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.lr_decay_step, gamma=args.lr_gamma)
    cur_best_pre_0 = 0.
    print('without pretraining.')

    run_time = 1

    loss_loger, pre_loger, rec_loger, ndcg_loger, hit_loger = [], [], [], [], []

    stopping_step = 0
    should_stop = False

    user_train1, beh_item_list = get_train_instances1(max_item_list, beh_label_list)

    nonshared_idx = -1

    for epoch in range(args.epoch):
        model.train()

        shuffle_indices = np.random.permutation(np.arange(len(user_train1)))
        user_train1 = user_train1[shuffle_indices]
        beh_item_list = [beh_item[shuffle_indices] for beh_item in beh_item_list]

        t1 = time()
        loss, rec_loss, emb_loss, ssl_loss, ssl2_loss = 0., 0., 0., 0., 0.

        n_batch = int(len(user_train1) / args.batch_size)

        # augment the graph
        aug_time = time()
        sub_mat = {}


        if args.aug_type in [0, 1]:
            sub_mat['sub1'] = model._convert_sp_mat_to_sp_tensor(augmentor.augment_adj_mat(aug_type=args.aug_type)) # 变成list
            sub_mat['sub2'] = model._convert_sp_mat_to_sp_tensor(augmentor.augment_adj_mat(aug_type=args.aug_type)) # 变成list
        else:
            for k in range(1, model.n_layers + 1):
                sub_mat['sub1%d' % k] = model._convert_sp_mat_to_sp_tensor(
                    augmentor.augment_adj_mat(aug_type=args.aug_type))
                sub_mat['sub2%d' % k] = model._convert_sp_mat_to_sp_tensor(
                    augmentor.augment_adj_mat(aug_type=args.aug_type))
        # print('aug time: %.1fs' % (time() - aug_time))

        iter_time = time()

        for idx in range(n_batch):
            optimizer.zero_grad()

            start_index = idx * args.batch_size
            end_index = min((idx + 1) * args.batch_size, len(user_train1))

            u_batch = user_train1[start_index:end_index]
            beh_batch = [beh_item[start_index:end_index] for beh_item in
                         beh_item_list]  # [[B, max_item1], [B, max_item2], [B, max_item3]]

            u_batch_list, i_batch_list = get_train_pairs(user_train_batch=u_batch,
                                                         beh_item_tgt_batch=beh_batch[-1])  # ndarray[N, ]  ndarray[N, ]

            # load into cuda
            u_batch = torch.from_numpy(u_batch).to(device)
            beh_batch = [torch.from_numpy(beh_item).to(device) for beh_item in beh_batch]
            # u_batch_indices = user_indices[u_batch_list].to(device)  # [B, N]
            # i_batch_indices = item_indices[i_batch_list].to(device)  # [B, N]
            u_batch_list = torch.from_numpy(u_batch_list).to(device)
            i_batch_list = torch.from_numpy(i_batch_list).to(device)

            model_time = time()
            ua_embeddings, ia_embeddings, rela_embeddings, attn_user, attn_item = model(sub_mat, device)
            # # print('model time: %.1fs' % (time() - model_time))
            # # argument ssl loss
            # batch_aru_ssl_loss_list = []
            # for i in eval(args.all_beh_idx):
            #     argument_ssl_loss = align_ssl_loss(u_batch_list, i_batch_list, ua_embeddings_sub1[:,i,:,:], ia_embeddings_sub1[:,i,:,:], ua_embeddings_sub2[:,i,:,:], ia_embeddings_sub2[:,i,:,:])
            #     batch_aru_ssl_loss_list.append(argument_ssl_loss)
            # batch_aru_ssl_loss = sum(batch_aru_ssl_loss_list)
            # # # behaviors ssl loss
            # # # ssl_loss_time = time()
            # batch_ssl2_loss_list = []
            # for aux_beh in eval(args.aux_beh_idx):
            #     aux_beh_ssl2_loss = diff_ssl_loss(u_batch_list, i_batch_list, ua_embeddings_align[:,-1,:,:],ia_embeddings_align[:,-1,:,:], ua_embeddings_align[:,i,:,:], ia_embeddings_align[:,i,:,:])
            #     batch_ssl2_loss_list.append(aux_beh_ssl2_loss)
            # batch_ssl2_loss = sum(batch_ssl2_loss_list)

           
            batch_rec_loss, batch_emb_loss = recloss(u_batch, beh_batch, ua_embeddings, ia_embeddings, rela_embeddings)
            # print('rec loss time: %.1fs' % (time() - rec_loss_time))

            batch_loss = batch_rec_loss + batch_emb_loss



            # if nonshared_idx == -1:
            #     batch_aru_ssl_loss.backward(retain_graph=True)
            #     for p_idx, p_name in enumerate(model.all_weights):
            #         print(p_name)
            #         p = model.all_weights[p_name]
            #         if p.grad is None or p.grad.equal(torch.zeros_like(p.grad)):
            #             nonshared_idx = p_idx
            #             break
            #     model.zero_grad()

            # hmg.step([batch_rec_loss] + batch_aru_ssl_loss_list + batch_ssl2_loss_list, nonshared_idx)
            batch_loss.backward()
            optimizer.step()

            loss += batch_loss.item() / n_batch
            rec_loss += batch_rec_loss.item() / n_batch
            emb_loss += batch_emb_loss.item() / n_batch
            # ssl_loss += batch_aru_ssl_loss.item() / n_batch
            # ssl2_loss += batch_ssl2_loss.item() / n_batch

        # print('iter time: %.1fs' % (time() - iter_time))
        if args.lr_decay: scheduler.step()
        torch.cuda.empty_cache()

        if np.isnan(loss) == True:
            print('ERROR: loss is nan.')
            sys.exit()

        # print the test evaluation metrics each 10 epochs; pos:neg = 1:10.
        if (epoch + 1) % args.test_epoch != 0:
            if args.verbose > 0 and epoch % args.verbose == 0:
                perf_str = 'Epoch %d [%.1fs]: train==[%.5f=%.5f + %.5f + %.5f + %.5f]' % (
                    epoch, time() - t1, loss, rec_loss, emb_loss, ssl_loss, ssl2_loss)
                print(perf_str)
            continue

        t2 = time()
        model.eval()
        with torch.no_grad():
            ua_embeddings, ia_embeddings, rela_embeddings, attn_user, attn_item = model(sub_mat, device)
            users_to_test = list(data_generator.test_set.keys())
            ret = test_torch(ua_embeddings[:, -1, :].detach().cpu().numpy(),
                             ia_embeddings[:, -1, :].detach().cpu().numpy(),
                             rela_embeddings[behs[-1]].detach().cpu().numpy(), users_to_test)

        t3 = time()

        loss_loger.append(loss)
        rec_loger.append(ret['recall'])
        pre_loger.append(ret['precision'])
        ndcg_loger.append(ret['ndcg'])
        hit_loger.append(ret['hit_ratio'])

        if args.verbose > 0:
            perf_str = 'Epoch %d [%.1fs + %.1fs]:, recall=[%.5f, %.5f], ' \
                       'precision=[%.5f, %.5f], hit=[%.5f, %.5f], ndcg=[%.5f, %.5f]' % \
                       (
                           epoch, t2 - t1, t3 - t2, ret['recall'][0],
                           ret['recall'][1],
                           ret['precision'][0], ret['precision'][1], ret['hit_ratio'][0], ret['hit_ratio'][1],
                           ret['ndcg'][0], ret['ndcg'][1])
            print(perf_str)

        cur_best_pre_0, stopping_step, should_stop, flag = early_stopping_new(ret['recall'][0], cur_best_pre_0,
                                                                              stopping_step, expected_order='acc',
                                                                              flag_step=10)
        # *********************************************************
        # early stopping when cur_best_pre_0 is decreasing for ten successive steps.
        if should_stop == True:
            break

    recs = np.array(rec_loger)
    pres = np.array(pre_loger)
    ndcgs = np.array(ndcg_loger)
    hit = np.array(hit_loger)

    best_rec_0 = max(recs[:, 0])
    idx = list(recs[:, 0]).index(best_rec_0)

    final_perf = "Best Iter=[%d]@[%.1f]\trecall=[%s], ndcg=[%s]" % \
                 (idx, time() - t0, '\t'.join(['%.4f' % r for r in recs[idx]]),
                  '\t'.join(['%.4f' % r for r in ndcgs[idx]]))
    print(final_perf)
