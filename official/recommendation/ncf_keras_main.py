# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""NCF framework to train and evaluate the NeuMF model.

The NeuMF model assembles both MF and MLP models under the NCF framework. Check
`neumf_model.py` for more details about the models.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

# pylint: disable=g-bad-import-order
from absl import app as absl_app
from absl import flags
import tensorflow as tf
# pylint: enable=g-bad-import-order

from official.datasets import movielens
from official.recommendation import constants as rconst
from official.recommendation import ncf_common
from official.recommendation import neumf_model
from official.utils.logs import logger
from official.utils.logs import mlperf_helper
from official.utils.misc import model_helpers
from official.utils.misc import distribution_utils


FLAGS = flags.FLAGS


def _keras_loss(y_true, y_pred):
  loss = tf.losses.sparse_softmax_cross_entropy(
      labels=tf.cast(y_true, tf.int32),
      logits=y_pred)
  return loss


def _get_metric_fn(params):
  batch_size = params["batch_size"]

  def metric_fn(y_true, y_pred):
    softmax_logits = y_pred[0, :]
    logits = tf.slice(softmax_logits, [0, 1], [batch_size, 1])

    dup_mask = tf.zeros([batch_size, 1])

    return _get_hit_rate_metric(
        logits,
        softmax_logits,
        dup_mask,
        params["num_neg"],
        params["match_mlperf"],
        params["use_xla_for_gpu"])

  return metric_fn


def _get_hit_rate_metric(
    logits,
    softmax_logits,
    dup_mask,
    num_neg,
    match_mlperf,
    use_xla_for_gpu):

  cross_entropy, metric_fn, in_top_k, ndcg, metric_weights =(
      neumf_model.compute_eval_loss_and_metrics_helper(
        logits,
        softmax_logits,
        dup_mask,
        num_neg,
        match_mlperf,
        use_tpu_spec=use_xla_for_gpu))

  return in_top_k


def _get_train_and_eval_data(producer, params):

  def preprocess_train_inpu(features, labels):
    features.pop(rconst.VALID_POINT_MASK)
    labels = tf.expand_dims(labels, -1)

    for k in features:
      tensor = features[k]
      features[k] = tf.expand_dims(tensor, -1)

    return features, labels

  train_input_fn = producer.make_input_fn(is_training=True)
  train_input_dataset = train_input_fn(params).map(
      preprocess_train_inpu)

  def preprocess_eval_input(features):
    features.pop(rconst.DUPLICATE_MASK)
    labels = tf.zeros_like(features[movielens.USER_COLUMN])
    labels = tf.expand_dims(labels, -1)

    for k in features:
      tensor = features[k]
      features[k] = tf.expand_dims(tensor, -1)

    return features, labels

  eval_input_fn = producer.make_input_fn(is_training=False)
  eval_input_dataset = eval_input_fn(params).map(
      lambda features : preprocess_eval_input(features))

  return train_input_dataset, eval_input_dataset


class IncrementEpochCallback(tf.keras.callbacks.Callback):
  """A callback to increase the requested epoch for the data producer.

  The reason why we need this is because we can only buffer a limited amount of data.
  So we keep a moving window to represent the buffer. This is to move the one of the
  window's boundaries for each epoch.
  """

  def __init__(self, producer):
    self._producer = producer

  def on_epoch_begin(self, epoch, logs=None):
    self._producer.increment_request_epoch()


def _get_keras_model(params):
  batch_size = params['batch_size']

  user_input = tf.keras.layers.Input(
      shape=(batch_size, 1), batch_size=1, name=movielens.USER_COLUMN, dtype=tf.int32)
  item_input = tf.keras.layers.Input(
      shape=(batch_size, 1), batch_size=1, name=movielens.ITEM_COLUMN, dtype=tf.int32)

  base_model = neumf_model.construct_model(
      user_input, item_input, params, need_strip=True)

  base_model_output = base_model.output

  logits= tf.keras.layers.Lambda(
      lambda x: tf.expand_dims(x, 0),
      name="logits")(base_model_output)

  zeros = tf.keras.layers.Lambda(
      lambda x: x * 0)(logits)

  softmax_logits = tf.keras.layers.concatenate(
      [zeros, logits],
      axis=-1)

  keras_model = tf.keras.Model(
      inputs=[user_input, item_input],
      outputs=softmax_logits)

  keras_model.summary()
  return keras_model


def run_ncf(_):
  """Run NCF training and eval with Keras."""
  params = ncf_common.parse_flags(FLAGS)

  num_users, num_items, num_train_steps, num_eval_steps, producer = (
      ncf_common.get_inputs(params))

  params["num_users"], params["num_items"] = num_users, num_items
  producer.start()
  model_helpers.apply_clean(flags.FLAGS)

  batches_per_step = params["batches_per_step"]
  train_input_dataset, eval_input_dataset = _get_train_and_eval_data(producer, params)
  train_input_dataset = train_input_dataset.batch(batches_per_step)
  eval_input_dataset = eval_input_dataset.batch(batches_per_step)

  strategy = ncf_common.get_distribution_strategy(params)
  # strategy = None
  with distribution_utils.MaybeDistributionScope(strategy):
    keras_model = _get_keras_model(params)
    optimizer = ncf_common.get_optimizer(params)

    keras_model.compile(
        loss=_keras_loss,
        metrics=[_get_metric_fn(params)],
        optimizer=optimizer)

    keras_model.fit(train_input_dataset,
        epochs=FLAGS.train_epochs,
        callbacks=[IncrementEpochCallback(producer)],
        verbose=2)

    tf.logging.info("Training done. Start evaluating")

    eval_results = keras_model.evaluate(
        eval_input_dataset,
        steps=num_eval_steps,
        verbose=2)

    tf.logging.info("Keras evaluation is done.")
    return eval_results


def main(_):
  with logger.benchmark_context(FLAGS), \
      mlperf_helper.LOGGER(FLAGS.output_ml_perf_compliance_logging):
    mlperf_helper.set_ncf_root(os.path.split(os.path.abspath(__file__))[0])
    if FLAGS.tpu:
      raise ValueError("NCF in Keras does not support TPU for now")
    run_ncf(FLAGS)


if __name__ == "__main__":
  tf.logging.set_verbosity(tf.logging.INFO)
  ncf_common.define_ncf_flags()
  absl_app.run(main)

