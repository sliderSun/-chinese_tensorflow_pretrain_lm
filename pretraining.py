#! -*- coding: utf-8 -*-
# 预训练脚本，多GPU版/TPU版本

import os

os.environ['TF_KERAS'] = '1'  # 必须使用tf.keras

from bert4keras.models import build_transformer_model
from bert4keras.backend import keras, K, search_layer
from bert4keras.optimizers import Adam
from bert4keras.optimizers import extend_with_weight_decay
from bert4keras.optimizers import extend_with_layer_adaptation
from bert4keras.optimizers import extend_with_piecewise_linear_lr
from bert4keras.optimizers import extend_with_gradient_accumulation
from keras.layers import Input, Lambda
from keras.models import Model
from keras.callbacks import EarlyStopping
from data_utils import *

model = 'roberta'

# 语料路径和模型保存路径
# 如果是TPU训练，那么语料必须存放在Google Cloud Storage上面，
# 路径必须以gs://开头；如果是GPU训练，改为普通路径即可。
# model_saved_path = 'gs://xxxx/bert4keras/saved_model/bert_model.ckpt'
model_saved_path = './saved_model/bert_model.ckpt'
best_model_saved_path = './saved_model_best/bert_model.ckpt'
model_saved_dir = './saved_model/'
corpus_paths = [
    './corpus_tfrecord/corpus.%s.tfrecord' % i for i in range(10)
]

# 其他配置
sequence_length = 512
batch_size = 4096
config_path = './model/bert_config.json'
checkpoint_path = './model/bert_model.ckpt'  # 如果从零训练，就设为None
learning_rate = 0.00176
weight_decay_rate = 0.01
num_warmup_steps = 3125
num_train_steps = 125000
steps_per_epoch = 10000
grad_accum_steps = 16  # 大于1即表明使用梯度累积
epochs = num_train_steps * grad_accum_steps // steps_per_epoch
exclude_from_weight_decay = ['Norm', 'bias']
exclude_from_layer_adaptation = ['Norm', 'bias']
# tpu_address = 'grpc://xxx.xxx.xxx.xxx:8470'  # 如果用多GPU跑，直接设为None
tpu_address = None  # 如果用多GPU跑，直接设为None
which_optimizer = 'lamb'  # adam 或 lamb，均自带weight decay
lr_schedule = {
    num_warmup_steps * grad_accum_steps: 1.0,
    num_train_steps * grad_accum_steps: 0.0,
}
floatx = K.floatx()

# 读取数据集，构建数据张量

if model == 'roberta':

    dataset = TrainingDatasetRoBERTa.load_tfrecord(
        record_names=corpus_paths,
        sequence_length=sequence_length,
        batch_size=batch_size // grad_accum_steps,
    )

elif model == 'spanbert':

    dataset = TrainingDatasetSpanBERT.load_tfrecord(
        record_names=corpus_paths,
        sequence_length=sequence_length,
        batch_size=batch_size // grad_accum_steps,
    )

elif model == 'gpt':

    dataset = TrainingDatasetGPT.load_tfrecord(
        record_names=corpus_paths,
        sequence_length=sequence_length,
        batch_size=batch_size // grad_accum_steps,
    )

elif model == 'unilm':

    dataset = TrainingDatasetUniLM.load_tfrecord(
        record_names=corpus_paths,
        sequence_length=sequence_length,
        batch_size=batch_size // grad_accum_steps,
        token_sep_id=3,  # 这里需要自己指定[SEP]的id
    )
else:
    dataset = TrainingDatasetBERT.load_tfrecord(
        record_names=corpus_paths,
        sequence_length=sequence_length,
        batch_size=batch_size // grad_accum_steps,
    )

def build_transformer_model_with_mlm_nsp():
    """带mlm和nsp的bert模型
    """
    bert = build_transformer_model(
        config_path, with_mlm='linear', return_keras_model=False
    )
    proba = bert.model.output

    # 辅助输入
    token_ids = Input(shape=(None,), dtype='int64', name='token_ids')  # 目标id
    is_masked = Input(shape=(None,), dtype=floatx, name='is_masked')  # mask标记
    nsp_label = Input(shape=(None,), dtype='int64', name='nsp')  # nsp

    def mlm_loss(inputs):
        """计算loss的函数，需要封装为一个层
        """
        y_true, y_pred, mask = inputs
        loss = K.sparse_categorical_crossentropy(
            y_true, y_pred, from_logits=True
        )
        loss = K.sum(loss * mask) / (K.sum(mask) + K.epsilon())
        return loss

    def nsp_loss(inputs):
        """计算nsp loss的函数，需要封装为一个层
        """
        y_true, y_pred = inputs
        loss = K.sparse_categorical_crossentropy(
            y_true, y_pred
        )
        loss = K.mean(loss)
        return loss

    def mlm_acc(inputs):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred, mask = inputs
        y_true = K.cast(y_true, floatx)
        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.sum(acc * mask) / (K.sum(mask) + K.epsilon())
        return acc

    def nsp_acc(inputs):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred = inputs
        y_pred, _ = y_pred
        y_true = K.cast(y_true, K.floatx)
        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.mean(acc)
        return acc

    mlm_loss = Lambda(mlm_loss, name='mlm_loss')([token_ids, proba, is_masked])
    mlm_acc = Lambda(mlm_acc, name='mlm_acc')([token_ids, proba, is_masked])
    nsp_loss = Lambda(nsp_loss, name='nsp_loss')([nsp_label, proba])
    nsp_acc = Lambda(nsp_acc, name='nsp_acc')([nsp_label, proba])

    train_model = Model(
        bert.model.inputs + [token_ids, is_masked], [mlm_loss, mlm_acc, nsp_loss, nsp_acc()]
    )

    loss = {
        'mlm_loss': lambda y_true, y_pred: y_pred,
        'mlm_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
        'nsp_loss': lambda y_true, y_pred: y_pred,
        'nsp_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
    }

    return bert, train_model, loss

def build_transformer_model_with_mlm():
    """带mlm的bert模型
    """
    bert = build_transformer_model(
        config_path, with_mlm='linear', return_keras_model=False
    )
    proba = bert.model.output

    # 辅助输入
    token_ids = Input(shape=(None,), dtype='int64', name='token_ids')  # 目标id
    is_masked = Input(shape=(None,), dtype=floatx, name='is_masked')  # mask标记

    def mlm_loss(inputs):
        """计算loss的函数，需要封装为一个层
        """
        y_true, y_pred, mask = inputs
        loss = K.sparse_categorical_crossentropy(
            y_true, y_pred, from_logits=True
        )
        loss = K.sum(loss * mask) / (K.sum(mask) + K.epsilon())
        return loss

    def mlm_acc(inputs):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred, mask = inputs
        y_true = K.cast(y_true, floatx)
        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.sum(acc * mask) / (K.sum(mask) + K.epsilon())
        return acc

    mlm_loss = Lambda(mlm_loss, name='mlm_loss')([token_ids, proba, is_masked])
    mlm_acc = Lambda(mlm_acc, name='mlm_acc')([token_ids, proba, is_masked])

    train_model = Model(
        bert.model.inputs + [token_ids, is_masked], [mlm_loss, mlm_acc]
    )

    loss = {
        'mlm_loss': lambda y_true, y_pred: y_pred,
        'mlm_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
    }

    return bert, train_model, loss


def build_transformer_model_with_lm():
    """带lm的bert模型
    """
    bert = build_transformer_model(
        config_path,
        with_mlm='linear',
        application='lm',
        return_keras_model=False
    )
    token_ids = bert.model.inputs[0]
    proba = bert.model.output

    def lm_loss(inputs, mask=None):
        """计算loss的函数，需要封装为一个层
        """
        y_true, y_pred = inputs
        y_true, y_pred = y_true[:, 1:], y_pred[:, :-1]

        if mask is None:
            mask = 1.0
        else:
            mask = K.cast(mask[1][:, 1:], floatx)

        loss = K.sparse_categorical_crossentropy(
            y_true, y_pred, from_logits=True
        )
        loss = K.sum(loss * mask) / (K.sum(mask) + K.epsilon())
        return loss

    def lm_acc(inputs, mask=None):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred = inputs
        y_true, y_pred = K.cast(y_true[:, 1:], floatx), y_pred[:, :-1]

        if mask is None:
            mask = 1.0
        else:
            mask = K.cast(mask[1][:, 1:], floatx)

        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.sum(acc * mask) / (K.sum(mask) + K.epsilon())
        return acc

    lm_loss = Lambda(lm_loss, name='lm_loss')([token_ids, proba])
    lm_acc = Lambda(lm_acc, name='lm_acc')([token_ids, proba])

    train_model = Model(bert.model.inputs, [lm_loss, lm_acc])

    loss = {
        'lm_loss': lambda y_true, y_pred: y_pred,
        'lm_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
    }

    return bert, train_model, loss


def build_transformer_model_with_unilm():
    """带unilm的bert模型
    """
    bert = build_transformer_model(
        config_path,
        with_mlm='linear',
        application='unilm',
        return_keras_model=False
    )
    token_ids = bert.model.inputs[0]
    segment_ids = bert.model.inputs[1]
    proba = bert.model.output

    def unilm_loss(inputs, mask=None):
        """计算loss的函数，需要封装为一个层
        """
        y_true, y_pred, segment_ids = inputs
        y_true, y_pred = y_true[:, 1:], y_pred[:, :-1]

        if mask is None:
            mask = 1.0
        else:
            mask = K.cast(mask[1][:, 1:], floatx)

        segment_ids = K.cast(segment_ids, floatx)
        mask = mask * segment_ids[:, 1:]

        loss = K.sparse_categorical_crossentropy(
            y_true, y_pred, from_logits=True
        )
        loss = K.sum(loss * mask) / (K.sum(mask) + K.epsilon())
        return loss

    def unilm_acc(inputs, mask=None):
        """计算准确率的函数，需要封装为一个层
        """
        y_true, y_pred, segment_ids = inputs
        y_true, y_pred = K.cast(y_true[:, 1:], floatx), y_pred[:, :-1]

        if mask is None:
            mask = 1.0
        else:
            mask = K.cast(mask[1][:, 1:], floatx)

        segment_ids = K.cast(segment_ids, floatx)
        mask = mask * segment_ids[:, 1:]

        acc = keras.metrics.sparse_categorical_accuracy(y_true, y_pred)
        acc = K.sum(acc * mask) / (K.sum(mask) + K.epsilon())
        return acc

    token_proba_segment = [token_ids, proba, segment_ids]
    unilm_loss = Lambda(unilm_loss, name='unilm_loss')(token_proba_segment)
    unilm_acc = Lambda(unilm_acc, name='unilm_acc')(token_proba_segment)

    train_model = Model(bert.model.inputs, [unilm_loss, unilm_acc])

    loss = {
        'unilm_loss': lambda y_true, y_pred: y_pred,
        'unilm_acc': lambda y_true, y_pred: K.stop_gradient(y_pred),
    }

    return bert, train_model, loss


def build_transformer_model_for_pretraining():
    """构建训练模型，通用于TPU/GPU
    注意全程要用keras标准的层写法，一些比较灵活的“移花接木”式的
    写法可能会在TPU上训练失败。此外，要注意的是TPU并非支持所有
    tensorflow算子，尤其不支持动态（变长）算子，因此编写相应运算
    时要格外留意。
    """
    if model == 'roberta':
        bert, train_model, loss = build_transformer_model_with_mlm()
    elif model == 'spanbert':
        bert, train_model, loss = build_transformer_model_with_mlm()
    elif model == 'gpt':
        bert, train_model, loss = build_transformer_model_with_lm()
    elif model == 'unilm':
        bert, train_model, loss = build_transformer_model_with_unilm()
    else:
        bert, train_model, loss = build_transformer_model_with_mlm_nsp()

    # 优化器
    optimizer = extend_with_weight_decay(Adam)
    if which_optimizer == 'lamb':
        optimizer = extend_with_layer_adaptation(optimizer)
    optimizer = extend_with_piecewise_linear_lr(optimizer)
    optimizer_params = {
        'learning_rate': learning_rate,
        'lr_schedule': lr_schedule,
        'weight_decay_rate': weight_decay_rate,
        'exclude_from_weight_decay': exclude_from_weight_decay,
        'exclude_from_layer_adaptation': exclude_from_layer_adaptation,
        'bias_correction': False,
    }
    if grad_accum_steps > 1:
        optimizer = extend_with_gradient_accumulation(optimizer)
        optimizer_params['grad_accum_steps'] = grad_accum_steps
    optimizer = optimizer(**optimizer_params)

    # 模型定型
    train_model.compile(loss=loss, optimizer=optimizer)

    # 如果传入权重，则加载。注：须在此处加载，才保证不报错。
    if checkpoint_path is not None:
        bert.load_weights_from_checkpoint(checkpoint_path)

    return bert, train_model


if tpu_address is None:
    # 单机多卡模式（多机多卡也类似，但需要硬软件配合，请参考https://tf.wiki）
    strategy = tf.distribute.MirroredStrategy()
else:
    # TPU模式
    resolver = tf.distribute.cluster_resolver.TPUClusterResolver(
        tpu=tpu_address
    )
    tf.config.experimental_connect_to_host(resolver.master())
    tf.tpu.experimental.initialize_tpu_system(resolver)
    strategy = tf.distribute.experimental.TPUStrategy(resolver)

with strategy.scope():
    bert, train_model = build_transformer_model_for_pretraining()
    train_model.summary()


class ModelCheckpoint(keras.callbacks.Callback):
    """自动保存最新模型
    """

    def __init__(self):
        self.loss = 1e6

    def on_epoch_end(self, epoch, logs=None):
        # model.save_weights 保存的模型，用 model.load_weights 加载
        # bert.save_weights_as_checkpoint 保存的模型，用 bert.load_weights_from_checkpoint 加载
        # 不要问为什么保存的模型用 build_transformer_model 加载不了
        # 先搞清楚对应情况，build_transformer_model 是用 load_weights_from_checkpoint 加载的。
        self.model.save_weights(model_saved_path, overwrite=True, save_format="tf")
        self.model.save_weights(os.path.join(model_saved_dir + "_{}".format(epoch), 'bert_model.ckpt'), overwrite=True)
        if logs['loss'] < self.loss:
            self.loss = logs['loss']
            bert.save_weights_as_checkpoint(best_model_saved_path)


# 保存模型
checkpoint = ModelCheckpoint()
# 记录日志
csv_logger = keras.callbacks.CSVLogger('training.log')

early_stopping = EarlyStopping(monitor='mlm_acc_loss', patience=10)


def adversarial_training(model, embedding_name, epsilon=1.0):
    """给模型添加对抗训练
    其中model是需要添加对抗训练的keras模型，embedding_name
    则是model里边Embedding层的名字。要在模型compile之后使用。
    """
    # if model.train_function is None:  # 如果还没有训练函数
    #     model._make_train_function()  # 手动make
    old_train_function = model.train_function  # 备份旧的训练函数

    # 查找Embedding层
    for output in model.outputs:
        embedding_layer = search_layer(output, embedding_name)
        if embedding_layer is not None:
            break
    if embedding_layer is None:
        raise Exception('Embedding layer not found')

    # 求Embedding梯度
    embeddings = embedding_layer.embeddings  # Embedding矩阵
    gradients = K.gradients(model.total_loss, [embeddings])  # Embedding梯度
    gradients = K.zeros_like(embeddings) + gradients[0]  # 转为dense tensor

    # 封装为函数
    inputs = (model._feed_inputs +
              model._feed_targets +
              model._feed_sample_weights)  # 所有输入层
    embedding_gradients = K.function(
        inputs=inputs,
        outputs=[gradients],
        name='embedding_gradients',
    )  # 封装为函数

    def train_function(inputs):  # 重新定义训练函数
        grads = embedding_gradients(inputs)[0]  # Embedding梯度
        delta = epsilon * grads / (np.sqrt((grads ** 2).sum()) + 1e-8)  # 计算扰动
        K.set_value(embeddings, K.eval(embeddings) + delta)  # 注入扰动
        outputs = old_train_function(inputs)  # 梯度下降
        K.set_value(embeddings, K.eval(embeddings) - delta)  # 删除扰动
        return outputs

    model.train_function = train_function  # 覆盖原训练函数


adversarial_training(train_model, 'Embedding-Token', 0.5)
# 模型训练
train_model.fit(
    dataset,
    steps_per_epoch=steps_per_epoch,
    epochs=epochs,
    callbacks=[checkpoint, csv_logger, early_stopping],
)
