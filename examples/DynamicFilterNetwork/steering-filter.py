#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: steering-filter.py

import argparse
import multiprocessing
import numpy as np
import cv2
import tensorflow as tf
from scipy.signal import convolve2d
from six.moves import range, zip

from tensorpack import *
from tensorpack.dataflow import dataset
from tensorpack.utils import logger
from tensorpack.utils.argtools import shape2d, shape4d
from tensorpack.utils.viz import *

BATCH = 32
SHAPE = 64


@layer_register()
def DynamicConvFilter(inputs, filters, out_channel,
                      kernel_shape,
                      stride=1,
                      padding='SAME'):
    """ see "Dynamic Filter Networks" (NIPS 2016)
        by Bert De Brabandere*, Xu Jia*, Tinne Tuytelaars and Luc Van Gool

    Remarks:
        This is the convolution version of a dynamic filter.

    Args:
        inputs : unfiltered input [b, h, w, 1] only grayscale images.
        filters : learned filters of [b, k, k, 1] (dynamically generated by the network).
        out_channel (int): number of output channel.
        kernel_shape: (h, w) tuple or a int.
        stride: (h, w) tuple or a int.
        padding (str): 'valid' or 'same'. Case insensitive.

    Returns
        tf.Tensor named ``output``.
    """

    # tf.unstack only works with known batch_size :-(
    batch_size, h, w, in_channel = inputs.get_shape().as_list()
    stride = shape4d(stride)

    inputs = tf.unstack(inputs)
    filters = tf.reshape(filters, [batch_size] + shape2d(kernel_shape) + [in_channel, out_channel])
    filters = tf.unstack(filters)

    # this is ok as TF uses the cuda stream context
    rsl = [tf.nn.conv2d(tf.reshape(d, [1, h, w, in_channel]),
                        tf.reshape(k, [kernel_shape, kernel_shape, in_channel, out_channel]),
                        stride, padding="SAME") for d, k in zip(inputs, filters)]
    rsl = tf.concat(rsl, axis=0, name='output')
    return rsl


class OnlineTensorboardExport(Callback):
    """Show learned filters for specific thetas in TensorBoard.
    """
    def __init__(self):
        # generate 32 filters (8 different, 4 times repeated)
        self.theta = np.array([i for _ in range(4) for i in range(8)]) / 8. * 2 * np.pi
        self.filters = np.array([ThetaImages.get_filter(t) for t in self.theta])
        self.cc = 0

    def _setup_graph(self):
        self.pred = self.trainer.get_predictor(['theta'], ['pred_filter'])

    def _trigger_epoch(self):
        def n(x):
            x -= x.min()
            x /= x.max()
            return x

        o = self.pred(self.theta)

        gt_filters = np.concatenate([self.filters[i, :, :] for i in range(8)], axis=0)
        pred_filters = np.concatenate([o[0][i, :, :, 0] for i in range(8)], axis=0)

        canvas = np.concatenate([n(gt_filters), n(pred_filters)], axis=1)
        l = canvas.shape[0] // 2
        canvas = np.concatenate([canvas[:l], canvas[l:]], axis=1)
        canvas = cv2.resize(canvas[..., None] * 255, (0, 0), fx=10, fy=10)

        self.trainer.monitors.put_image('filter_export', canvas)
        # # you might also want to write these images to disk (as in the casestudy from the docs)
        # cv2.imwrite("export/out%04i.jpg" % self.cc, canvas)
        # self.cc += 1


class Model(ModelDesc):
    def inputs(self):
        return [tf.TensorSpec((BATCH, ), tf.float32, 'theta'),
                tf.TensorSpec((BATCH, SHAPE, SHAPE), tf.float32, 'image'),
                tf.TensorSpec((BATCH, SHAPE, SHAPE), tf.float32, 'gt_image'),
                tf.TensorSpec((BATCH, 9, 9), tf.float32, 'gt_filter')]

    def _parameter_net(self, theta, kernel_shape=9):
        """Estimate filters for convolution layers

        Args:
            theta: angle of filter
            kernel_shape: size of each filter

        Returns:
            learned filter as [B, k, k, 1]
        """
        with argscope(FullyConnected, nl=tf.nn.leaky_relu):
            net = FullyConnected('fc1', theta, 64)
            net = FullyConnected('fc2', net, 128)

        pred_filter = FullyConnected('fc3', net, kernel_shape ** 2, nl=tf.identity)
        pred_filter = tf.reshape(pred_filter, [BATCH, kernel_shape, kernel_shape, 1], name="pred_filter")
        logger.info('Parameter net output: {}'.format(pred_filter.get_shape().as_list()))
        return pred_filter

    def build_graph(self, theta, image, gt_image, gt_filter):
        kernel_size = 9

        theta = tf.reshape(theta, [BATCH, 1, 1, 1]) - np.pi
        image = tf.reshape(image, [BATCH, SHAPE, SHAPE, 1])
        gt_image = tf.reshape(gt_image, [BATCH, SHAPE, SHAPE, 1])

        pred_filter = self._parameter_net(theta)
        pred_image = DynamicConvFilter('dfn', image, pred_filter, 1, kernel_size)

        with tf.name_scope('visualization'):
            pred_filter = tf.reshape(pred_filter, [BATCH, kernel_size, kernel_size, 1])
            gt_filter = tf.reshape(gt_filter, [BATCH, kernel_size, kernel_size, 1])

            filters = tf.concat([pred_filter, gt_filter], axis=2, name="filters")
            images = tf.concat([pred_image, gt_image], axis=2, name="images")
        tf.summary.image('pred_gt_filters', filters, max_outputs=20)
        tf.summary.image('pred_gt_images', images, max_outputs=20)

        cost = tf.reduce_mean(tf.squared_difference(pred_image, gt_image), name="cost")
        summary.add_moving_summary(cost)
        return cost

    def optimizer(self):
        return tf.train.AdamOptimizer(1e-3)


class ThetaImages(ProxyDataFlow, RNGDataFlow):
    @staticmethod
    def get_filter(theta, sigma=1., filter_size=9):
        x = np.arange(-filter_size // 2 + 1, filter_size // 2 + 1)
        g = np.array([np.exp(-(x**2) / (2 * sigma**2))])
        gp = np.array([-(x / sigma) * np.exp(-(x**2) / (2 * sigma**2))])

        gt_filter = np.matmul(g.T, gp)
        gt_filter = np.cos(theta) * gt_filter + np.sin(theta) * gt_filter.T

        return gt_filter

    @staticmethod
    def filter_with_theta(image, theta, sigma=1., filter_size=9):
        """Implements a steerable Gaussian filter.

        This function can be used to evaluate the first
        directional derivative of an image, using the
        method outlined in

            W. T. Freeman and E. H. Adelson, "The Design
            and Use of Steerable Filters", IEEE PAMI, 1991.

        It evaluates the directional derivative of the input
        image I, oriented at THETA degrees with respect to the
        image rows. The standard deviation of the Gaussian kernel
        is given by SIGMA (assumed to be equal to unity by default).

        Args:
            image: any input image (only one channel)
            theta: orientation of filter [0, 2 * pi]
            sigma (float, optional): standard derivation of Gaussian
            filter_size (int, optional): filter support

        Returns:
            filtered image and the filter
        """
        x = np.arange(-filter_size // 2 + 1, filter_size // 2 + 1)
        # 1D Gaussian
        g = np.array([np.exp(-(x**2) / (2 * sigma**2))])
        # first-derivative of 1D Gaussian
        gp = np.array([-(x / sigma) * np.exp(-(x**2) / (2 * sigma**2))])

        ix = convolve2d(image, -gp, mode='same', boundary='fill', fillvalue=0)
        ix = convolve2d(ix, g.T, mode='same', boundary='fill', fillvalue=0)

        iy = convolve2d(image, g, mode='same', boundary='fill', fillvalue=0)
        iy = convolve2d(iy, -gp.T, mode='same', boundary='fill', fillvalue=0)

        output = np.cos(theta) * ix + np.sin(theta) * iy

        # np.cos(theta) * np.matmul(g.T, gp) + np.sin(theta) * np.matmul(gp.T, g)
        gt_filter = np.matmul(g.T, gp)
        gt_filter = np.cos(theta) * gt_filter + np.sin(theta) * gt_filter.T

        return output, gt_filter

    def __init__(self, ds):
        ProxyDataFlow.__init__(self, ds)

    def reset_state(self):
        ProxyDataFlow.reset_state(self)
        RNGDataFlow.reset_state(self)

    def __iter__(self):
        for image, label in self.ds:
            theta = self.rng.uniform(0, 2 * np.pi)
            filtered_image, gt_filter = ThetaImages.filter_with_theta(image, theta)
            yield [theta, image, filtered_image, gt_filter]


def get_data():
    # probably not the best dataset
    ds = dataset.BSDS500('train', shuffle=True)
    ds = AugmentImageComponent(ds, [imgaug.Grayscale(keepdims=False),
                                    imgaug.Resize((SHAPE, SHAPE))])
    ds = ThetaImages(ds)
    ds = RepeatedData(ds, 50)  # just pretend this dataset is bigger
    # this pre-computation is pretty heavy
    ds = PrefetchDataZMQ(ds, min(20, multiprocessing.cpu_count()))
    ds = BatchData(ds, BATCH)
    return ds


def get_config():
    logger.auto_set_dir()
    dataset_train = get_data()

    return TrainConfig(
        dataflow=dataset_train,
        callbacks=[
            ModelSaver(),
            OnlineTensorboardExport()
        ],
        model=Model(),
        steps_per_epoch=len(dataset_train),
        max_epoch=50,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.', required=True)
    parser.add_argument('--load', help='load model')
    args = parser.parse_args()

    with change_gpu(args.gpu):
        NGPU = len(args.gpu.split(','))
        config = get_config()
        if args.load:
            config.session_init = SaverRestore(args.load)
        launch_train_with_config(config, SyncMultiGPUTrainer(NGPU))
