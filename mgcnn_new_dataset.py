##
## Molecular Graph Convolutional Neural Networks for alkaloid biosynthesis pathway prediction
## Licensed under The MIT License [see LICENSE for details]
## Written by Ryohei Eguchi and Naoaki ONO
##

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import sys
import numpy as np
import pandas as pd
import tensorflow as tf
import deepchem as dc
import pickle
import tempfile
import random

from sklearn.model_selection import KFold
from deepchem.models.tensorgraph.layers import Dense, SoftMax, SoftMaxCrossEntropy, WeightedError, Stack
from deepchem.models.tensorgraph.layers import Label, Weights, Feature, GraphConv, BatchNorm, Dropout
from deepchem.models.tensorgraph.layers import GraphPool, GraphGather, ReduceMean
from deepchem.models.tensorgraph.tensor_graph import TensorGraph
from deepchem.metrics import to_one_hot
from deepchem.feat.mol_graphs import ConvMol

## Fix a random seed if needed
np.random.seed(123)
tf.set_random_seed(123)

## Load Alkaloids data
field_task15 = ['C00073', 'C00078', 'C00079', 'C00082', 'C00235', 'C00341', 'C00353',
                                        'C00448', 'C01789', 'C03506', 'C00047', 'C00108', 'C00187', 'C00148',
                                        'C00041', 'C00129', 'C00062', 'C01852', 'C00049', 'C00135', 'C00223',
                                        'C00509', 'C00540', 'C01477', 'C05903', 'C05904', 'C05905', 'C05908',
                                        'C09762']
ntask = len(field_task15)
field_smiles = 'smiles'
path_alkaloid_data = 'data/train_valid.csv'
dc_featurizer = dc.feat.ConvMolFeaturizer()
dc_loader = dc.data.data_loader.CSVLoader(tasks=field_task15, smiles_field=field_smiles, featurizer=dc_featurizer)
dataset_train = dc_loader.featurize(path_alkaloid_data)
dc_transformer = dc.trans.BalancingTransformer(transform_w=True, dataset=dataset_train)
dataset_train = dc_transformer.transform(dataset_train)
nd = len(dataset_train)

dataset_test = dc_loader.featurize("data/test.csv")
dc_transformer = dc.trans.BalancingTransformer(transform_w=True, dataset=dataset_test)
dataset_test = dc_transformer.transform(dataset_test)

n_batch = 50

## Setup Input Features
atom_features = Feature(shape=(None, 75))
degree_slice = Feature(shape=(None, 2), dtype=tf.int32)
membership = Feature(shape=(None,), dtype=tf.int32)

# mol = ConvMol.agglomerate_mols(dataset_all.X)
# ndeg = len(mol.get_deg_adjacency_lists())
ndeg = 11
deg_adjs = []
for ii in range(1, 11):
    deg_adj = Feature(shape=(None, ii), dtype=tf.int32)
    deg_adjs.append(deg_adj)

label15 = []
for ts in range(ntask):
    label_t = Label(shape=(None, 2))
    label15.append(label_t)


def construct_model():
    ## Setup Graph Convolution Network
    tg = TensorGraph(use_queue=False, learning_rate=0.001, model_dir='ckpt_2')

    gc1 = GraphConv(64, activation_fn=tf.nn.relu, in_layers=[atom_features, degree_slice, membership] + deg_adjs)
    bn1 = BatchNorm(in_layers=[gc1])
    gp1 = GraphPool(in_layers=[bn1, degree_slice, membership] + deg_adjs)
    dp1 = Dropout(0.2, in_layers=gp1)

    gc2 = GraphConv(64, activation_fn=tf.nn.relu, in_layers=[dp1, degree_slice, membership] + deg_adjs)
    bn2 = BatchNorm(in_layers=[gc2])
    gp2 = GraphPool(in_layers=[bn2, degree_slice, membership] + deg_adjs)
    dp2 = Dropout(0.5, in_layers=gp2)

    gc3 = GraphConv(64, activation_fn=tf.nn.relu, in_layers=[dp2, degree_slice, membership] + deg_adjs)
    bn3 = BatchNorm(in_layers=[gc3])
    gp3 = GraphPool(in_layers=[bn3, degree_slice, membership] + deg_adjs)
    dp3 = Dropout(0.5, in_layers=gp3)

    dense1 = Dense(out_channels=128, activation_fn=tf.nn.relu, in_layers=[dp3])
    out1 = GraphGather(batch_size=n_batch, activation_fn=tf.nn.tanh,
                       in_layers=[dense1, degree_slice, membership] + deg_adjs)

    # in this model, multilabel (15 precursors) shall be classified
    # using the trained featuret vector
    cost15 = []
    for ts in range(ntask):
        label_t = label15[ts]
        classification_t = Dense(out_channels=2, in_layers=[out1])
        softmax_t = SoftMax(in_layers=[classification_t])
        tg.add_output(softmax_t)
        cost_t = SoftMaxCrossEntropy(in_layers=[label_t, classification_t])
        cost15.append(cost_t)

    # The loss function is the average of the 15 crossentropy
    loss = ReduceMean(in_layers=cost15)
    tg.set_loss(loss)
    tg.build()
    return tg


def data_generator(dataset, n_epoch=1, predict=False):
    for ee in range(n_epoch):
        if not predict:
            print('Starting epoch %i' % ee)
        for ind, (X_b, y_b, w_b, ids_b) in enumerate(
                dataset.iterbatches(n_batch, pad_batches=True, deterministic=True)):
            fd = {}
            for ts, label_t in enumerate(label15):
                fd[label_t] = to_one_hot(y_b[:, ts])
            mol = ConvMol.agglomerate_mols(X_b)
            fd[atom_features] = mol.get_atom_features()
            fd[degree_slice] = mol.deg_slice
            fd[membership] = mol.membership
            deg_adj_list = mol.get_deg_adjacency_lists()
            for ii in range(1, 11):
                fd[deg_adjs[ii - 1]] = deg_adj_list[ii]
            yield fd


def accuracy_multi_molecules(prediction, yyy, th=0.5):
    ny, ns = yyy.shape
    conf_mat_list = []
    accu_list = []
    recall_list = []
    precision_list = []
    f1_score_list = []
    mcc_list = []
    for ss in range(ns):
        tp = np.sum(np.logical_and(yyy[0:ny, ss] == 1, prediction[ss][0:ny, 1] > th))
        fp = np.sum(np.logical_and(yyy[0:ny, ss] == 0, prediction[ss][0:ny, 1] > th))
        fn = np.sum(np.logical_and(yyy[0:ny, ss] == 1, prediction[ss][0:ny, 1] < th))
        tn = np.sum(np.logical_and(yyy[0:ny, ss] == 0, prediction[ss][0:ny, 1] < th))
        conf_mat_list.append(np.array([[tp, fp], [fn, tn]]))
        accu_list.append((tp + tn) / ny)
        recall = tp / (tp + fn)
        precision = tp / (tp + fp)
        recall_list.append(recall)
        precision_list.append(precision)
        f1_score = 2 * recall * precision / (recall + precision)
        f1_score_list.append(f1_score)
        mcc = (tp * tn - fp * fn) / np.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
        mcc_list.append(mcc)

    return [conf_mat_list, accu_list, recall_list, precision_list, f1_score_list, mcc_list]


## Main
n_epoch = 300
tg = construct_model()

tg.fit_generator(data_generator(dataset_train, n_epoch=n_epoch), restore=False)
pred_cv = tg.predict_on_generator(data_generator(dataset_test, predict=True))
conf_cv, accu_cv, recall_cv, precision_cv, f1_score_cv, mcc_score_cv = accuracy_multi_molecules(pred_cv,
                                                                                                dataset_test.y)


# write results in pandas dataframe
df_results = pd.DataFrame()
df_results['conf_mat'] = conf_cv
df_results['accuracy'] = accu_cv
df_results['recall'] = recall_cv
df_results['precision'] = precision_cv
df_results['f1_score'] = f1_score_cv
df_results['mcc'] = mcc_score_cv

df_results.to_csv('results_new_dataset.csv', index=False)