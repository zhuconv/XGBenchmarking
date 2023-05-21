import copy
from sklearn.metrics import roc_auc_score
import numpy as np
import torch
from torch_geometric.data import Data, Batch
from abc import ABC


class BaseEvaluation(ABC):

    # def collect_batch(self, x_labels, weights, data, signal_class, x_level):
    #     pass

    def reset(self):
        pass

    def eval_epoch(self):
        pass


def partial_weights(node_weights, pos_neg):

    pos_weights = copy.deepcopy(node_weights)
    neg_weights = copy.deepcopy(1 - node_weights)

    pos_idx = torch.where(pos_weights == 1)[0]
    neg_idx = torch.where(neg_weights == 1)[0]

    pos_zero_num = int((1-pos_neg[0]) * pos_weights.sum().item())
    neg_zero_num = int((1-pos_neg[1]) * neg_weights.sum().item())
    np.random.seed(99)
    torch.cuda.manual_seed(99)
    torch.manual_seed(99)

    pos_idx = pos_idx[torch.randperm(pos_idx.size(0))[:pos_zero_num]]
    neg_idx = neg_idx[torch.randperm(neg_idx.size(0))[:neg_zero_num]]

    weights = torch.ones_like(node_weights)
    weights[pos_idx] = 0
    weights[neg_idx] = 0
    return weights


class LabelFidelity(BaseEvaluation):
    def __init__(self, model, pos_neg=(1, 0), target=None, type='acc'):
        self.perf = []
        self.valid = []
        self.test = []
        self.type = type
        self.pos_neg = pos_neg
        self.name = self.type+str(pos_neg)+'-fidelity@'
        self.sparsity_list = []
        self.target = target
        self.classifier = model
        self.device = next(model.parameters()).device
    # def update_name(self, dataset):
    #     self.name +=

    def control_pos_neg(self, weights):
        if self.target:
            if self.pos_neg[0] == 0:
                neg_frac = 1 - weights.mean().item()
                neg = self.target / neg_frac
                pos = 0
                if neg > 1:
                    pos = (self.target - neg_frac) / (1 - neg_frac)
                    neg = 1
                pos_neg = (pos, neg)
            elif self.pos_neg[0] == 1:
                pos_frac = weights.mean().item()
                neg = (self.target - pos_frac) / (1 - pos_frac)
                pos = 1
                if neg < 0:
                    neg = 0
                    pos = self.target / pos_frac
                pos_neg = (pos, neg)
            else:
                ValueError(f"{self.pos_neg} at position 0 should be 0/1.")
        else:
            pos_neg = self.pos_neg
        return pos_neg

    def create_new_data_and_sparsity(self, data, weights, weight_type='edge'):
        sum_ = 0
        data_list = []
        count = 0
        for graph in data.to_data_list():
            if weight_type == 'node':
                add_nodes = graph.num_nodes
                node_weights = weights[sum_:sum_ + add_nodes]
                sum_ += add_nodes

                pos_neg = self.control_pos_neg(node_weights)
                node_weights =partial_weights(node_weights, pos_neg)

                # assert abs(node_weights.mean().item()-self.target) < 0.15

                self.sparsity_list.append(node_weights.squeeze())

                idx = node_weights.reshape(-1).nonzero().reshape(-1)
                x = graph.x[idx]
                if graph.pos is not None:
                    pos = graph.pos[idx]
                else:
                    pos = None


                edge_list = []
                for edge_pair in graph.edge_index.T:
                    edge_list += [edge_pair] if edge_pair[0] in idx and edge_pair[1] in idx else []
                if edge_list:
                    edge_index = torch.vstack(edge_list).T
                else:
                    edge_index = torch.tensor([], dtype=graph.edge_index.dtype, device=graph.edge_index.device).reshape(2, -1)

                if graph.edge_attr is not None:
                    edge_attr_list = []
                    for i, edge_pair in enumerate(graph.edge_index.T):
                        edge_attr_list += [graph.edge_attr[i]] if edge_pair[0] in idx and edge_pair[1] in idx else []
                    edge_attr = torch.vstack(edge_attr_list)
                else:
                    edge_attr = None


                row = edge_index[0]
                node_idx = row.new_full((max(idx)+1,), -1)
                # print(node_idx.device)
                # print(idx.device)
                node_idx[idx] = torch.arange(idx.size(0), device=idx.device)
                edge_index = node_idx[edge_index]

            else:
                add_edges = graph.num_edges
                graph_weights = weights[sum_:sum_ + add_edges]
                sum_ += add_edges

                pos_neg = self.control_pos_neg(graph_weights)
                graph_weights = partial_weights(graph_weights, pos_neg)
                # try:
                # assert torch.allclose(graph_weights.mean(), torch.tensor(self.target))
                # except:
                #     pass
                self.sparsity_list.append(graph_weights.squeeze())

                idx = graph_weights.reshape(-1).nonzero().reshape(-1)

                assert idx.numel()

                x = graph.x
                edge_index = graph.edge_index[:, idx]
                edge_attr = graph.edge_attr[idx] if graph.edge_attr is not None else None

                # node relabel
                num_nodes = x.size(0)
                sub_nodes = torch.unique(edge_index)

                x = x[sub_nodes]

                if graph.pos is not None:
                    pos = graph.pos
                    pos = pos[sub_nodes]
                else:
                    pos = None

                row, col = edge_index
                # remapping the nodes in the explanatory subgraph to new ids.
                node_idx = row.new_full((num_nodes,), -1)
                node_idx[sub_nodes] = torch.arange(sub_nodes.size(0), device=row.device)
                edge_index = node_idx[edge_index]

            data_list += [Data(x=x, y=graph.y, pos=pos, edge_index=edge_index, edge_attr=edge_attr)]
        if "-fidelity" in self.name and count:
            print(f'There is {count} graphs with no edges in this batch. (len:{len(data_list)})')
        batch_data = Batch.from_data_list(data_list)

        return batch_data

    def collect_batch(self, x_labels, weights, data, signal_class, x_level):
        pos_data_list = []
        for item in data.to_data_list():
            if (1 - item.node_label).sum() == 0:  # there are all positive nodes that is useless
                continue
            pos_data_list += [item] if item.y.item() == signal_class else []
        pos_data = Batch.from_data_list(pos_data_list)

        if hasattr(data, "edge_label"):  # level == graph
            weights = pos_data.edge_label.reshape(-1, 1)
        elif x_level == 'geometric':
            weights = pos_data.node_label.reshape(-1, 1)
        else:
            assert x_level == 'graph'
            node_weights = pos_data.node_label.reshape(-1, 1)
            weights = node_attn_to_edge_attn(node_weights, pos_data.edge_index)

        # if weights.shape[0] != pos_data.edge_index.shape[1]:    # node_weights
        #     weights = node_attn_to_edge_attn(weights, pos_data.edge_index)
        weight_type = 'node' if weights.shape[0] != pos_data.edge_index.shape[1] else 'edge'

        new_data = self.create_new_data_and_sparsity(pos_data, weights, weight_type=weight_type)

        with torch.no_grad():
            origin_logits = self.classifier(pos_data.to(self.device))
            masked_logits = self.classifier(new_data.to(self.device))

        # masked_logits = classifier(data, edge_attr=data.edge_attr, edge_attn=weights)
        clf_labels = pos_data.y.clone()

        if self.type == 'prob':
            clf_labels[clf_labels == 0] = -1

            origin_pred = origin_logits.sigmoid()
            masked_pred = masked_logits.sigmoid()
            scores = (origin_pred - masked_pred) * clf_labels

        else:
            assert self.type == 'acc'
            origin_pred = (origin_logits.sigmoid() > 0.5).float()
            masked_pred = (masked_logits.sigmoid() > 0.5).float()

            scores = (origin_pred == clf_labels).float() - (masked_pred == clf_labels).float()

        self.perf.append(scores.reshape(-1))
        return scores.reshape(-1).mean().item()

    def eval_epoch(self):
        # in the phas 'train', the train_res will be -1
        if "@0." in self.name:
            pass
        else:
            sparsity = torch.cat(self.sparsity_list).mean().item()
            self.name += f"{sparsity:.2f}"

        if not self.perf:
            return -1
        else:
            # print(self.perf)
            perf = torch.cat(self.perf).cpu()
        return perf.mean().item()


    def reset(self):
        self.perf = []

    def update_epoch(self, valid_res, test_res):
        self.valid.append(valid_res)
        self.test.append(test_res) if not test_res else None
        return self.valid, self.test


class FidelEvaluation(BaseEvaluation):
    def __init__(self, model, sparsity, type='acc', symbol='+', instance='all'):
        self.sparsity = sparsity
        self.perf = []
        self.valid = []
        self.test = []
        self.type = type
        self.symbol = symbol
        self.name = self.type+'-fidelity'+symbol+'@'+str(sparsity)+'-'+instance
        self.instance = instance
        self.classifier = model
        self.device = next(model.parameters()).device

    def create_new_data(self, data, weights, weight_type='edge', signal_class=None, instance=None):
        sum_ = 0
        data_list = []
        count = 0
        pos_data_list = []
        for graph in data.to_data_list():
            if weight_type == 'node':
                node_weights = weights[sum_:sum_ + graph.num_nodes]
                sum_ += graph.num_nodes
                if instance == 'pos' and graph.y.item() != signal_class:
                    continue
                if instance == 'neg' and graph.y.item() == signal_class:
                    continue

                node_weights = control_sparsity(node_weights, self.sparsity, self.symbol)
                idx = node_weights.reshape(-1).nonzero().reshape(-1)
                assert idx.numel()
                edge_list = []
                # print(node_weights)
                for edge_pair in graph.edge_index.T:
                    edge_list += [edge_pair] if edge_pair[0] in idx and edge_pair[1] in idx else []
                if edge_list:
                    edge_index = torch.vstack(edge_list).T
                else:
                    edge_index = torch.tensor([], dtype=graph.edge_index.dtype, device=graph.edge_index.device).reshape(2, -1)

                if graph.edge_attr is not None:
                    edge_attr_list = [graph.edge_attr[i] if (idx == edge_pair[0]).numel() and (idx == edge_pair[1]).numel()
                                      else None for i, edge_pair in enumerate(graph.edge_index.T)]
                    edge_attr = torch.vstack(edge_attr_list)
                else:
                    edge_attr = None

                x = graph.x[idx]
                if graph.pos is not None:
                    pos = graph.pos[idx]
                else:
                    pos = None

                row = edge_index[0]
                node_idx = row.new_full((max(idx)+1,), -1)
                node_idx[idx] = torch.arange(idx.size(0), device=idx.device)
                edge_index = node_idx[edge_index]
            else:
                graph_weights = weights[sum_:sum_ + graph.num_edges]
                sum_ += graph.num_edges
                if instance == 'pos' and graph.y.item() != signal_class:
                    continue
                if instance == 'neg' and graph.y.item() == signal_class:
                    continue

                graph_weights = control_sparsity(graph_weights, self.sparsity, self.symbol)
                idx = graph_weights.reshape(-1).nonzero().reshape(-1)
                assert idx.numel()
                x = graph.x
                edge_index = graph.edge_index[:, idx]
                edge_attr = graph.edge_attr[idx] if graph.edge_attr is not None else None

                # node relabel
                num_nodes = x.size(0)
                sub_nodes = torch.unique(edge_index)

                x = x[sub_nodes]

                if graph.pos is not None:
                    pos = graph.pos
                    pos = pos[sub_nodes]
                else:
                    pos = None


                row, col = edge_index
                # remapping the nodes in the explanatory subgraph to new ids.
                node_idx = row.new_full((num_nodes,), -1)
                node_idx[sub_nodes] = torch.arange(sub_nodes.size(0), device=row.device)
                edge_index = node_idx[edge_index]

            data_list += [Data(x=x, y=graph.y, pos=pos, edge_index=edge_index, edge_attr=edge_attr)]
            pos_data_list += [graph]
        if "-fidelity" in self.name and count:
            print(f'There is {count} graphs with no edges in this batch. (len:{len(data_list)})')
        new_data = Batch.from_data_list(data_list)
        pos_data = Batch.from_data_list(pos_data_list)
        return pos_data, new_data

    def collect_batch(self, x_labels, weights, data, signal_class, x_level):
        # data.edge_index = self.classifier.get_emb(data)[1]
        # print(data.edge_index is None)
        weights = weights.reshape(-1, 1)
        # pos_data, pos_new_data = self.get_pos_instances(data, weights, weight_type=weight_type)
        if hasattr(data, "edge_label"):
            weight_type = 'edge'
        elif x_level == 'geometric':
            weight_type = 'node'
        else:
            assert x_level == 'graph'
            weights = node_attn_to_edge_attn(weights, data.edge_index)
            weight_type = 'edge'
        # weight_type = 'node' if weights.shape[0] != data.edge_index.shape[1] else 'edge'
        # print(data.x.device, weights.device)
        pos_data, pos_new_data = self.create_new_data(data, weights, weight_type=weight_type, signal_class=signal_class, instance=self.instance)

        # if weights.shape[0] != data.edge_index.shape[1]:    # node_weights
        #     weights = node_attn_to_edge_attn(weights, data.edge_index)
        with torch.no_grad():
            origin_logits = self.classifier(pos_data.to(self.device))
            masked_logits = self.classifier(pos_new_data.to(self.device))

        clf_labels = pos_data.y.clone()

        if self.type == 'prob':
            clf_labels[clf_labels == 0] = -1

            origin_pred = origin_logits.sigmoid()
            masked_pred = masked_logits.sigmoid()
            scores = (origin_pred - masked_pred) * clf_labels

        else:
            assert self.type == 'acc'
            # assert self.type == 'acc'
            origin_pred = (origin_logits.sigmoid() > 0.5).float()
            masked_pred = (masked_logits.sigmoid() > 0.5).float()
            scores = (origin_pred == clf_labels).float() - (masked_pred == clf_labels).float()

        self.perf.append(scores.reshape(-1))
        # try:
            # this is used for visualization
            # scores.item() == 1
        # except:
        #     pass
        return scores.reshape(-1).mean().item()

    def eval_epoch(self):
        # in the phas 'train', the train_res will be -1
        if not self.perf:
            return -1
        else:
            perf = torch.cat(self.perf).cpu()
        return perf.mean().item()

    def reset(self):
        self.perf = []

    def update_epoch(self, valid_res, test_res):
        self.valid.append(valid_res)
        self.test.append(test_res) if not test_res else None

        return self.valid, self.test


class TOPKEvaluation(BaseEvaluation):
    """
    the class will produce a precision for each graph with signal_class
    """

    def __init__(self, topk):
        self.att = []
        self.valid = []
        self.test = []
        self.k = topk
        self.scale = 'instance'
        self.name = 'top'+str(topk)
        self.count = 0
        self.total = 0

    def collect_batch(self, x_labels, att, data, signal_class, x_level):
        x_labels, att, belongs_graph_id = get_signal_class(x_labels, att, data, signal_class)
        self.total += len(belongs_graph_id.unique())
        for i in belongs_graph_id.unique():
            labels_i = x_labels[belongs_graph_id == i]
            att_i = att[belongs_graph_id == i]
            if labels_i.sum() < self.k:
                self.count += 1
                continue
            topk_idx = np.argsort(-att_i)[:self.k]
            node_topk_i = labels_i[topk_idx]
            prec_i = node_topk_i.sum().item() / self.k
            self.perf.append(prec_i)
        return self.perf

    def eval_epoch(self):
        if not self.count == 0:
            print(f"There are {self.count}/{self.total} graphs has less than {self.k} important nodes/edges.")
        if not self.perf:
            return -1
        else:
            perf = self.perf.cpu()
            return sum(perf) / len(perf)

    def reset(self):
        self.perf = []
        self.count = 0


    def update_epoch(self, valid_res, test_res):
        self.valid.append(valid_res)
        self.test.append(test_res) if not test_res else None
        return self.valid, self.test

    def get_score(self, explanations):
        pass


class AUCEvaluation(BaseEvaluation):
    """
    A class enabling the evaluation of the AUC metric on both graphs and nodes.

    :param ground_truth: ground truth labels.
    :param indices: Which indices to evaluate.

    :funcion get_score: obtain the roc auc score.
    """

    def __init__(self):
        self.att = []
        self.gnd = []
        self.valid = []
        self.test = []
        self.scale = 'dataset'
        self.name = 'exp_auc'

    def collect_batch(self, x_labels, node_att, data, signal_class, x_level):
        x_labels, node_att, _ = get_signal_class(x_labels, node_att, data, signal_class)
        self.att.append(node_att)
        self.gnd.append(x_labels)
        return roc_auc_score(x_labels.cpu(), node_att.cpu())


    def eval_epoch(self, return_att=False):
        # in the phase 'train', the train_res will be -1
        if not self.att:
            return -1
        else:
            att = torch.cat(self.att)
            label = torch.cat(self.gnd)
            return roc_auc_score(label.cpu(), att.cpu()) if not return_att else (att.cpu(), label.cpu())

    def reset(self):
        self.att = []
        self.gnd = []


    def update_epoch(self, valid_res, test_res):
        self.valid.append(valid_res)
        self.test.append(test_res) if not test_res else None
        return self.valid, self.test


def get_signal_class(x_labels, att, data, signal_class):
    # regard edges as a node set
    node_id = data.batch.cpu()
    edge_id = node_id[data.edge_index[0]] if hasattr(data, 'edge_label') else None
    if len(x_labels) == data.num_nodes:
        ids = node_id
    elif len(x_labels) == data.num_edges:
        ids = edge_id

    graph_label = data.y.cpu()
    in_signal_class = (graph_label[ids] == signal_class).reshape(-1)

    return x_labels[in_signal_class], att[in_signal_class], ids[in_signal_class]


def control_sparsity(mask, sparsity=None, symbol='+'):
        r"""

        :param mask: mask that need to transform
        :param sparsity: sparsity_list we need to control i.e. 0.7, 0.5
        :return: transformed mask where top 1 - sparsity_list values are set to inf.
        """
        if sparsity is None:
            sparsity = 0.7

        # if len(mask.shape)>1:
        #     mask = mask.squeeze()
        _, indices = torch.sort(mask, dim=0, descending=True)
        mask_len = mask.shape[0]
        split_point = int((1 - sparsity) * mask_len)
        important_indices = indices[: split_point]
        unimportant_indices = indices[split_point:]
        trans_mask = mask.clone()
        if symbol == "+":  # larger indicates batter
            trans_mask[important_indices] = 0
            trans_mask[unimportant_indices] = 1
        else:
            assert symbol == '-' #lower indicates better
            trans_mask[important_indices] = 1
            trans_mask[unimportant_indices] = 0


        return trans_mask


def node_attn_to_edge_attn(node_attn, edge_index):
    src_attn = node_attn[edge_index[0]]
    dst_attn = node_attn[edge_index[1]]
    edge_attn = src_attn * dst_attn
    return edge_attn