"""

Reference: Weiping Song et al. "AutoInt: Automatic Feature Interaction Learning via Self-Attentive Neural Networks"
           (https://arxiv.org/pdf/1810.11921.pdf)

author: massquantity

"""
import time
from itertools import islice
import numpy as np
import tensorflow as tf
from tensorflow.python.keras.initializers import truncated_normal
from .base import Base, TfMixin
from ..evaluate.evaluate import EvalMixin
from ..utils.tf_ops import (
    reg_config,
    dropout_config,
    lr_decay_config
)
from ..data.data_generator import DataGenFeat
from ..utils.sampling import NegativeSampling
from ..utils.unique_features import (
    get_predict_indices_and_values,
    get_recommend_indices_and_values
)


class AutoInt(Base, TfMixin, EvalMixin):
    def __init__(self, task, data_info=None, embed_size=16, att_embed_size=None,
                 num_heads=2, use_residual=True, n_epochs=20, lr=0.001,
                 lr_decay=False, reg=None, batch_size=256, num_neg=1,
                 use_bn=True, dropout_rate=None, hidden_units="128,64,32",
                 batch_sampling=False, seed=42, lower_upper_bound=None,
                 tf_sess_config=None):

        Base.__init__(self, task, data_info, lower_upper_bound)
        TfMixin.__init__(self, tf_sess_config)
        EvalMixin.__init__(self, task)

        self.task = task
        self.data_info = data_info
        self.embed_size = embed_size
        # 'att_embed_size' also decides the num of attention layer
        (self.att_embed_size,
         self.att_layer_num) = self._att_config(att_embed_size)
        self.num_heads = num_heads
        self.use_residual = use_residual
        self.n_epochs = n_epochs
        self.lr = lr
        self.lr_decay = lr_decay
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.num_neg = num_neg
        self.use_bn = use_bn
        self.dropout_rate = dropout_config(dropout_rate)
        self.batch_sampling = batch_sampling
        self.hidden_units = list(map(int, hidden_units.split(",")))
        self.n_users = data_info.n_users
        self.n_items = data_info.n_items
        self.global_mean = data_info.global_mean
        self.default_prediction = (data_info.global_mean if (
                task == "rating") else 0.0)
        self.seed = seed
        self.user_consumed = None
        self.sparse = self._decide_sparse_indices(data_info)
        self.dense = self._decide_dense_values(data_info)
        if self.sparse:
            self.sparse_feature_size = self._sparse_feat_size(data_info)
            self.sparse_field_size = self._sparse_field_size(data_info)
        if self.dense:
            self.dense_field_size = self._dense_field_size(data_info)

    def _build_model(self):
        tf.set_random_seed(self.seed)
        self.labels = tf.placeholder(tf.float32, shape=[None])
        self.is_training = tf.placeholder_with_default(False, shape=[])
        self.concat_embed = []

        self._build_user_item()
        if self.sparse:
            self._build_sparse()
        if self.dense:
            self._build_dense()

        attention_layer = tf.concat(self.concat_embed, axis=1)
        for i in range(self.att_layer_num):
            attention_layer = self.multi_head_attention(
                attention_layer, self.att_embed_size[i])
        attention_layer = tf.layers.flatten(attention_layer)
        self.output = tf.squeeze(tf.layers.dense(attention_layer, units=1))

    def _build_user_item(self):
        self.user_indices = tf.placeholder(tf.int32, shape=[None])
        self.item_indices = tf.placeholder(tf.int32, shape=[None])

        user_feat = tf.get_variable(
            name="user_feat",
            shape=[self.n_users, self.embed_size],
            initializer=truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        item_feat = tf.get_variable(
            name="item_feat",
            shape=[self.n_items, self.embed_size],
            initializer=truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        user_embed = tf.expand_dims(
            tf.nn.embedding_lookup(user_feat, self.user_indices), axis=1)
        item_embed = tf.expand_dims(
            tf.nn.embedding_lookup(item_feat, self.item_indices), axis=1)
        self.concat_embed.extend([user_embed, item_embed])

    def _build_sparse(self):
        self.sparse_indices = tf.placeholder(
            tf.int32, shape=[None, self.sparse_field_size])

        sparse_feat = tf.get_variable(
            name="sparse_feat",
            shape=[self.sparse_feature_size, self.embed_size],
            initializer=truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        sparse_embed = tf.nn.embedding_lookup(sparse_feat, self.sparse_indices)
        self.concat_embed.append(sparse_embed)

    def _build_dense(self):
        self.dense_values = tf.placeholder(
            tf.float32, shape=[None, self.dense_field_size])
        dense_values_reshape = tf.reshape(
            self.dense_values, [-1, self.dense_field_size, 1])

        dense_feat = tf.get_variable(
            name="dense_feat",
            shape=[self.dense_field_size, self.embed_size],
            initializer=truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        batch_size = tf.shape(self.dense_values)[0]
        # 1 * F_dense * K
        dense_embed = tf.expand_dims(dense_feat, axis=0)
        # B * F_dense * K
        dense_embed = tf.tile(dense_embed, [batch_size, 1, 1])
        dense_embed = tf.multiply(dense_embed, dense_values_reshape)
        self.concat_embed.append(dense_embed)

    # inputs: B * F * Ki, new_embed_size: K, num_heads: H
    def multi_head_attention(self, inputs, new_embed_size):
        multi_embed_size = self.num_heads * new_embed_size
        # B * F * (K*H)
        queries = tf.layers.dense(inputs=inputs,
                                  units=multi_embed_size,
                                  activation=None,
                                  kernel_initializer=truncated_normal(
                                      0.0, 0.01),
                                  use_bias=False)
        keys = tf.layers.dense(inputs=inputs,
                               units=multi_embed_size,
                               activation=None,
                               kernel_initializer=truncated_normal(
                                   0.0, 0.01),
                               use_bias=False)
        values = tf.layers.dense(inputs=inputs,
                                 units=multi_embed_size,
                                 activation=None,
                                 kernel_initializer=truncated_normal(
                                     0.0, 0.01),
                                 use_bias=False)
        if self.use_residual:
            residual = tf.layers.dense(inputs=inputs,
                                       units=multi_embed_size,
                                       activation=None,
                                       kernel_initializer=truncated_normal(
                                           0.0, 0.01),
                                       use_bias=False)

        # H * B * F * K
        queries = tf.stack(tf.split(queries, self.num_heads, axis=2))
        keys = tf.stack(tf.split(keys, self.num_heads, axis=2))
        values = tf.stack(tf.split(values, self.num_heads, axis=2))

        # H * B * F * F
        weights = queries @ tf.transpose(keys, [0, 1, 3, 2])
        # weights = weights / np.sqrt(new_embed_size)
        weights = tf.nn.softmax(weights)
        # H * B * F * K
        outputs = weights @ values
        # 1 * B * F * (K*H)
        outputs = tf.concat(tf.split(outputs, self.num_heads, axis=0), axis=-1)
        # B * F * (K*H)
        outputs = tf.squeeze(outputs, axis=0)
        if self.use_residual:
            outputs += residual
        outputs = tf.nn.relu(outputs)
        return outputs

    def _build_train_ops(self, global_steps=None):
        if self.task == "rating":
            self.loss = tf.losses.mean_squared_error(labels=self.labels,
                                                     predictions=self.output)
        elif self.task == "ranking":
            self.loss = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(labels=self.labels,
                                                        logits=self.output)
            )

        if self.reg is not None:
            reg_keys = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
            total_loss = self.loss + tf.add_n(reg_keys)
        else:
            total_loss = self.loss

        optimizer = tf.train.AdamOptimizer(self.lr)
        optimizer_op = optimizer.minimize(total_loss, global_step=global_steps)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        self.training_op = tf.group([optimizer_op, update_ops])
        self.sess.run(tf.global_variables_initializer())

    def fit(self, train_data, verbose=1, shuffle=True,
            eval_data=None, metrics=None, **kwargs):
        self.show_start_time()
        self.user_consumed = train_data.user_consumed
        if self.lr_decay:
            n_batches = int(len(train_data) / self.batch_size)
            self.lr, global_steps = lr_decay_config(self.lr, n_batches,
                                                    **kwargs)
        else:
            global_steps = None

        self._build_model()
        self._build_train_ops(global_steps)

        if self.task == "ranking" and self.batch_sampling:
            self._check_has_sampled(train_data, verbose)
            data_generator = NegativeSampling(train_data,
                                              self.data_info,
                                              self.num_neg,
                                              self.sparse,
                                              self.dense,
                                              batch_sampling=True)

        else:
            data_generator = DataGenFeat(train_data,
                                         self.sparse,
                                         self.dense)

        self.train_feat(data_generator, verbose, shuffle, eval_data, metrics)

    def predict(self, user, item):
        user = np.asarray(
            [user]) if isinstance(user, int) else np.asarray(user)
        item = np.asarray(
            [item]) if isinstance(item, int) else np.asarray(item)

        unknown_num, unknown_index, user, item = self._check_unknown(
            user, item)

        (user_indices,
         item_indices,
         sparse_indices,
         dense_values) = get_predict_indices_and_values(
            self.data_info, user, item, self.n_items, self.sparse, self.dense)
        feed_dict = self._get_feed_dict(user_indices, item_indices,
                                        sparse_indices, dense_values,
                                        None, False)

        preds = self.sess.run(self.output, feed_dict)
        if self.task == "rating":
            preds = np.clip(preds, self.lower_bound, self.upper_bound)
        elif self.task == "ranking":
            preds = 1 / (1 + np.exp(-preds))

        if unknown_num > 0:
            preds[unknown_index] = self.default_prediction

        return preds

    def recommend_user(self, user, n_rec, **kwargs):
        user = self._check_unknown_user(user)
        if not user:
            return   # popular ?

        (user_indices,
         item_indices,
         sparse_indices,
         dense_values) = get_recommend_indices_and_values(
            self.data_info, user, self.n_items, self.sparse, self.dense)
        feed_dict = self._get_feed_dict(user_indices, item_indices,
                                        sparse_indices, dense_values,
                                        None, False)

        recos = self.sess.run(self.output, feed_dict)
        if self.task == "ranking":
            recos = 1 / (1 + np.exp(-recos))

        consumed = self.user_consumed[user]
        count = n_rec + len(consumed)
        ids = np.argpartition(recos, -count)[-count:]
        rank = sorted(zip(ids, recos[ids]), key=lambda x: -x[1])
        return list(
            islice(
                (rec for rec in rank if rec[0] not in consumed), n_rec
            )
        )

    @staticmethod
    def _att_config(att_embed_size):
        if not att_embed_size:
            att_embed_size = (8, 8, 8)
            att_layer_num = 3
        elif isinstance(att_embed_size, int):
            att_embed_size = [att_embed_size]
            att_layer_num = 1
        elif isinstance(att_embed_size, (list, tuple)):
            att_layer_num = len(att_embed_size)
        return att_embed_size, att_layer_num


