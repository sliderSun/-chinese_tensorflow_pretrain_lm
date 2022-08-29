"""
@Time : 2022/8/29 22:27 
@Author : sunshb10145 
@File : classification_baseline.py.py 
@desc:
tnews baseline:
bert-12 acc: 56.6
tnews: (https://github.com/CLUEbenchmark/CLUE)
"""
import json
from bert4keras.layers import *
from bert4keras.models import build_transformer_model
from bert4keras.optimizers import Adam
from bert4keras.snippets import DataGenerator
from bert4keras.tokenizers import Tokenizer
from keras_preprocessing.sequence import pad_sequences
from tqdm import tqdm

num_classes = 16
maxlen = 64
batch_size = 32
epochs = 5
num_hidden_layers = 12
lr = 1e-5

# BERT base
config_path = '../model/bert_config.json'
checkpoint_path = '../model/bert_model.ckpt'
dict_path = '../model/vocab.txt'


def load_data(filename):
    D = []
    with open(filename) as f:
        for i, l in enumerate(f):
            l = json.loads(l)
            text, label, label_des = l['sentence'], l['label'], l['label_desc']
            label = int(label) - 100 if int(label) < 105 else int(label) - 101
            D.append((text, int(label), label_des))
    return D


# 加载数据集
train_data = load_data(
    './tnews_public/train.json'
)
valid_data = load_data(
    './tnews_public/dev.json'
)

# tokenizer
tokenizer = Tokenizer(dict_path, do_lower_case=True)


class data_generator(DataGenerator):
    def __iter__(self, shuffle=False):
        batch_token_ids, batch_segment_ids, batch_labels = [], [], []
        for is_end, (text, label, label_desc) in self.sample(shuffle):
            token_ids, segment_ids = tokenizer.encode(text, maxlen=maxlen)
            batch_token_ids.append(token_ids)
            batch_segment_ids.append(segment_ids)
            batch_labels.append([int(label)])

            if len(batch_token_ids) == self.batch_size or is_end:
                batch_token_ids = pad_sequences(batch_token_ids)
                batch_segment_ids = pad_sequences(batch_segment_ids)
                batch_labels = pad_sequences(batch_labels)

                yield [batch_token_ids, batch_segment_ids], batch_labels

                batch_token_ids, batch_segment_ids, batch_labels = [], [], []


train_generator = data_generator(data=train_data, batch_size=batch_size)
val_generator = data_generator(valid_data, batch_size)

# build model
bert = build_transformer_model(config_path=config_path,
                               checkpoint_path=checkpoint_path,
                               num_hidden_layers=num_hidden_layers)
output = Lambda(lambda x: x[:, 0])(bert.output)
output = Dense(num_classes, activation='softmax')(output)

model = Model(bert.inputs, output)
model.summary()

model.compile(loss='sparse_categorical_crossentropy',
              optimizer=Adam(lr),
              metrics=['acc'])


def evaluate(data):
    total, right = 0., 0.
    for x_true, y_true in tqdm(data):
        y_pred = model.predict(x_true).argmax(axis=1)
        y_true = y_true[:, 0]
        total += len(y_true)
        right += (y_true == y_pred).sum()

    return right / total


class Evaluator(keras.callbacks.Callback):
    def __init__(self):
        self.best_acc = 0.

    def on_epoch_end(self, epoch, logs=None):
        acc = evaluate(val_generator)
        if acc > self.best_acc:
            self.best_acc = acc
            self.model.save_weights('best_baseline.weights')
        print('acc: {}, best acc: {}'.format(acc, self.best_acc))


if __name__ == '__main__':
    evaluator = Evaluator()
    model.fit_generator(train_generator.forfit(),
                        steps_per_epoch=len(train_generator),
                        epochs=epochs,
                        callbacks=[evaluator])
else:
    model.load_weights('best_baseline.weights')
