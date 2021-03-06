#! /usr/bin/env python

import datetime
import os
import time

import data_helpers
import numpy as np
import pandas as pd
import tensorflow as tf
from sklearn.metrics import f1_score
from tensorflow.contrib import learn
from text_cnn import TextCNN

# Parameters
# ==================================================

# Data loading params
tf.flags.DEFINE_string("data_base_path", "../../../data/processed/", "Base data path.")
tf.flags.DEFINE_string("data_file_test", "../../../data/processed/in-cat-test.csv", "Test data source.")
tf.flags.DEFINE_string("data_file_valid", "../../../data/processed/out-of-cat-valid.csv", "Validation data source.")

# Pre trained Embedding parameter
tf.flags.DEFINE_string("embedding_path", "../../../models/w2v-amz.bin",
                       "path to the file containing the pretrained embedding.")
tf.flags.DEFINE_string("embedding_bin", True, "True if the file is in binary format.")

# Model Hyperparameters
tf.flags.DEFINE_integer("embedding_dim", 20, "Dimensionality of character embedding (default: 300)")
tf.flags.DEFINE_string("filter_sizes", "3,4,5", "Comma-separated filter sizes (default: '3,4,5')")
tf.flags.DEFINE_integer("num_filters", 3, "Number of filters per filter size (default: 128)")
tf.flags.DEFINE_float("dropout_keep_prob", 0.7, "Dropout keep probability (default: 0.7)")
tf.flags.DEFINE_float("l2_reg_lambda", 0.0, "L2 regularization lambda (default: 0.0)")

# Training parameters
tf.flags.DEFINE_integer("batch_size", 32, "Batch Size (default: 32)")
tf.flags.DEFINE_integer("num_epochs", 20, "Number of training epochs (default: 20)")
tf.flags.DEFINE_integer("evaluate_every", 50, "Evaluate model on dev set after this many steps (default: 100)")
tf.flags.DEFINE_integer("checkpoint_every", 100, "Save model after this many steps (default: 100)")
tf.flags.DEFINE_integer("num_checkpoints", 5, "Number of checkpoints to store (default: 5)")
# Misc Parameters
tf.flags.DEFINE_boolean("allow_soft_placement", True, "Allow device soft device placement")
tf.flags.DEFINE_boolean("log_device_placement", False, "Log placement of ops on devices")

FLAGS = tf.flags.FLAGS
# FLAGS._parse_flags()
# print("\nParameters:")
# for attr, value in sorted(FLAGS.__flags.items()):
#     print("{}={}".format(attr.upper(), value))
# print("")

# global variables
TEST_SAMPLES = 0
TEST_CORRECT = 0


def preprocess(current_fold):
    # Data Preparation
    # ==================================================

    # Load data
    print("Loading data...")
    x_train, x_test, x_valid, y_train, y_test, y_valid, df_valid = data_helpers.load_all_data_and_labels(FLAGS.data_base_path,
                                                                                               current_fold,
                                                                                               FLAGS.data_file_test,
                                                                                               FLAGS.data_file_valid)
    # Build vocabulary
    # vll brauchen wir hier train und test
    max_document_length = max([len(x.split(" ")) for x in x_train])
    vocab_processor = learn.preprocessing.VocabularyProcessor(max_document_length)
    x_train = np.array(list(vocab_processor.fit_transform(x_train)))
    x_test = np.array(list(vocab_processor.fit_transform(x_test)))
    x_valid = np.array(list(vocab_processor.fit_transform(x_valid)))

    # for category assessment
    df_valid['text'] = pd.Series(x_valid.tolist())
    df_valid['labels'] = pd.Series(y_valid.tolist())
    print(df_valid['labels'][0])
    print(type(y_valid))

    # Randomly shuffle train data
    np.random.seed(10)
    shuffle_indices = np.random.permutation(np.arange(len(y_train)))
    x_train = x_train[shuffle_indices]
    y_train = y_train[shuffle_indices]

    print("Vocabulary Size: {:d}".format(len(vocab_processor.vocabulary_)))
    print("Train/Test/Val split: {:d}/{:d}/{:d}".format(len(y_train), len(y_test), len(y_valid)))
    return x_train, x_test, x_valid, y_train, y_test, y_valid, vocab_processor, df_valid


def train(x_train, y_train, vocab_processor, x_test, y_test, x_valid, y_valid, report_df, current_fold, df_valid):
    # Training
    # ==================================================

    with tf.Graph().as_default():
        session_conf = tf.ConfigProto(
            allow_soft_placement=FLAGS.allow_soft_placement,
            log_device_placement=FLAGS.log_device_placement)
        sess = tf.Session(config=session_conf)
        with sess.as_default():
            cnn = TextCNN(
                sequence_length=x_train.shape[1],
                num_classes=y_train.shape[1],
                vocab_size=len(vocab_processor.vocabulary_),
                embedding_size=FLAGS.embedding_dim,
                filter_sizes=list(map(int, FLAGS.filter_sizes.split(","))),
                num_filters=FLAGS.num_filters,
                l2_reg_lambda=FLAGS.l2_reg_lambda)

            # Define Training procedure
            global_step = tf.Variable(0, name="global_step", trainable=False)
            optimizer = tf.train.AdamOptimizer(1e-3)
            grads_and_vars = optimizer.compute_gradients(cnn.loss)
            train_op = optimizer.apply_gradients(grads_and_vars, global_step=global_step)

            # Keep track of gradient values and sparsity (optional)
            grad_summaries = []
            for g, v in grads_and_vars:
                if g is not None:
                    grad_hist_summary = tf.summary.histogram("{}/grad/hist".format(v.name), g)
                    sparsity_summary = tf.summary.scalar("{}/grad/sparsity".format(v.name), tf.nn.zero_fraction(g))
                    grad_summaries.append(grad_hist_summary)
                    grad_summaries.append(sparsity_summary)
            grad_summaries_merged = tf.summary.merge(grad_summaries)

            # Output directory for models and summaries
            timestamp = str(int(time.time()))
            out_dir = os.path.abspath(os.path.join(os.path.curdir, "runs", timestamp))
            print("Writing to {}\n".format(out_dir))

            # Summaries for loss and accuracy
            loss_summary = tf.summary.scalar("loss", cnn.loss)
            acc_summary = tf.summary.scalar("accuracy", cnn.accuracy)

            # Train Summaries
            train_summary_op = tf.summary.merge([loss_summary, acc_summary, grad_summaries_merged])
            train_summary_dir = os.path.join(out_dir, "summaries", "train")
            train_summary_writer = tf.summary.FileWriter(train_summary_dir, sess.graph)

            # Dev summaries
            dev_summary_op = tf.summary.merge([loss_summary, acc_summary])
            dev_summary_dir = os.path.join(out_dir, "summaries", "dev")
            dev_summary_writer = tf.summary.FileWriter(dev_summary_dir, sess.graph)

            # Checkpoint directory. Tensorflow assumes this directory already exists so we need to create it
            checkpoint_dir = os.path.abspath(os.path.join(out_dir, "checkpoints"))
            checkpoint_prefix = os.path.join(checkpoint_dir, "model")
            if not os.path.exists(checkpoint_dir):
                os.makedirs(checkpoint_dir)
            saver = tf.train.Saver(tf.global_variables(), max_to_keep=FLAGS.num_checkpoints)

            # Write vocabulary
            vocab_processor.save(os.path.join(out_dir, "vocab"))

            # Initialize all variables
            sess.run(tf.global_variables_initializer())

            ## code by Sven
            vocabulary = vocab_processor.vocabulary_
            initW = None
            # load embedding vectors from the word2vec
            print("Load word2vec file {}".format(FLAGS.embedding_path))
            initW = data_helpers.load_embedding_vectors_word2vec(vocabulary,
                                                                 FLAGS.embedding_path,
                                                                 FLAGS.embedding_bin)
            print("word2vec file has been loaded")
            sess.run(cnn.W.assign(initW))

            ## end of change

            def train_step(x_batch, y_batch):
                """
                A single training step
                """
                feed_dict = {
                    cnn.input_x: x_batch,
                    cnn.input_y: y_batch,
                    cnn.dropout_keep_prob: FLAGS.dropout_keep_prob
                }
                _, step, summaries, loss, accuracy = sess.run(
                    [train_op, global_step, train_summary_op, cnn.loss, cnn.accuracy],
                    feed_dict)
                time_str = datetime.datetime.now().isoformat()
                print("{}: step {}, loss {:g}, acc {:g}".format(time_str, step, loss, accuracy))
                train_summary_writer.add_summary(summaries, step)

            def dev_step(x_batch, y_batch, writer=None):
                """
                Evaluates model on a dev set
                """
                feed_dict = {
                    cnn.input_x: x_batch,
                    cnn.input_y: y_batch,
                    cnn.dropout_keep_prob: 1.0
                }
                step, summaries, loss, accuracy = sess.run(
                    [global_step, dev_summary_op, cnn.loss, cnn.accuracy],
                    feed_dict)

                # compute f1 score
                labels = np.array(y_batch)[:, 1]
                prediction = cnn.predictions.eval(feed_dict)
                f1 = f1_score(labels, prediction)

                time_str = datetime.datetime.now().isoformat()
                print("{}: step {}, loss {:g}, acc {:g}  f1 {:g}".format(time_str, step, loss, accuracy, f1))
                if writer:
                    writer.add_summary(summaries, step)
                return accuracy, f1

            # Generate batches
            batches = data_helpers.batch_iter(
                list(zip(x_train, y_train)), FLAGS.batch_size, FLAGS.num_epochs)
            # Training loop. For each batch...
            for batch in batches:
                x_batch, y_batch = zip(*batch)
                train_step(x_batch, y_batch)
                current_step = tf.train.global_step(sess, global_step)
                if current_step % FLAGS.evaluate_every == 0:
                    print("\nTest evaluation:")
                    dev_step(x_test, y_test, writer=dev_summary_writer)
                    print("")
                if current_step % FLAGS.checkpoint_every == 0:
                    path = saver.save(sess, checkpoint_prefix, global_step=current_step)
                    print("Saved model checkpoint to {}\n".format(path))

            print("\nEnd test evaluation:")
            acc_score_in_cat, f1_score_in_cat = dev_step(x_test, y_test, writer=dev_summary_writer)
            print("\nEnd val evaluation:")
            acc_score_out_of_cat, f1_score_out_of_cat = dev_step(x_valid, y_valid, writer=dev_summary_writer)

            cat_dict = {}
            for category in df_valid.category.unique():
                mask = df_valid['category'] == category
                category_df = df_valid[mask]
                category_array = np.array(category_df['text'].values.tolist())
                category_label = np.array(category_df['labels'].values.tolist())
                print(category_array.shape)
                print(x_valid.shape)
                print(category_label.shape)
                print(y_valid.shape)
                cat_acc, cat_f1 = dev_step(category_array, category_label)
                cat_dict[category + "-f1"] = cat_f1
                cat_dict[category + "-acc"] = cat_acc

            name = 'fold-' + str(current_fold)
            report_df.append([name, acc_score_in_cat, f1_score_in_cat, acc_score_out_of_cat, f1_score_out_of_cat] + list(cat_dict.values()))

            return list(cat_dict.keys())

            # print("saving trained model")
            # save_path = saver.save(sess, "model.ckpt", global_step=current_step)
            # print("Model saved in path: %s" % save_path)


def main(argv=None):
    report_df = []
    for i in range(10):
        x_train, x_test, x_valid, y_train, y_test, y_valid, vocab_processor, df_valid = preprocess(i)
        category_columns = train(x_train, y_train, vocab_processor, x_test, y_test, x_valid, y_valid, report_df, i, df_valid)

    report_df = pd.DataFrame(report_df, columns=['name', 'acc_in_cat', 'f1_in_cat', 'acc_out_of_cat', 'f1_out_of_cat'] + category_columns)

    print("evaluation in cat")
    print(f"acc: {report_df['acc_in_cat'].mean()}, f1: {report_df['f1_in_cat'].mean()}")

    print("evaluation out of cat")
    print(f"acc: {report_df['acc_out_of_cat'].mean()}, f1: {report_df['f1_out_of_cat'].mean()}")

    report_df.to_csv('../../../reports/timoshenko-results.csv', index=False)


if __name__ == '__main__':
    tf.app.run()
