from __future__ import print_function
import tensorflow as tf
from tensorflow.contrib.layers import batch_norm, fully_connected, flatten
from tensorflow.contrib.layers import xavier_initializer
from ops import *
import numpy as np

class ActionGenerator(object):
    def __init__(self, segan):
        self.segan = segan
        self.grucell = tf.contrib.rnn.GRUCell(256+self.segan.accent_class)
    def zero(self,batch_size):
        if self.cell_type == "grucell" or not hasattr(self, "grucell"): 
            grucell = self.grucell
        else :
            return ValueError("No such implemented Cell for action sampling")
        return grucell.zero_state(batch_size, tf.float32)
    def __call__(self, noisy_w,hidden_state, is_ref, spk=None, z_on=False, do_prelu=False):
        # TODO: remove c_vec
        """ Build the graph propagating (noisy_w) --> x
        On first pass will make variables.
        """
        segan = self.segan
        def make_z(shape, mean=0., std=1., name='z'):
            if is_ref:
                with tf.variable_scope(name) as scope:
                    z_init = tf.random_normal_initializer(mean=mean, stddev=std)
                    z = tf.get_variable("z", shape,
                                        initializer=z_init,
                                        trainable=False
                                        )
                    if z.device != "/device:GPU:0":
                        # this has to be created into gpu0
                        print('z.device is {}'.format(z.device))
                        assert False
            else:
                z = tf.random_normal(shape, mean=mean, stddev=std,
                                     name=name, dtype=tf.float32)
            return z

        if hasattr(segan, 'generator_built'):
            tf.get_variable_scope().reuse_variables()
            make_vars = False
        else:
            make_vars = True
        if is_ref:
            print('*** Building Generator ***')
        in_dims = noisy_w.get_shape().as_list()
        h_i = noisy_w
        if len(in_dims) == 2:
            h_i = tf.expand_dims(noisy_w, -1)
        elif len(in_dims) < 2 or len(in_dims) > 3:
            raise ValueError('Generator input must be 2-D or 3-D')
        kwidth = 31
        enc_layers = 7
        skips = []
        if is_ref and do_prelu:
            #keep track of prelu activations
            alphas = []
        with tf.variable_scope('g_e'):
            #AE to be built is shaped:
            # enc ~ [16384x1, 8192x16, 4096x32, 2048x32, 1024x64, 512x64, 256x128, 128x128, 64x256, 32x256, 16x512, 8x1024]
            # dec ~ [8x2048, 16x1024, 32x512, 64x512, 8x256, 256x256, 512x128, 1024x128, 2048x64, 4096x64, 8192x32, 16384x1]
            #FIRST ENCODER
            for layer_idx, layer_depth in enumerate(segan.g_enc_depths):
                bias_init = None
                if segan.bias_downconv:
                    if is_ref:
                        print('Biasing downconv in G')
                    bias_init = tf.constant_initializer(0.)
                h_i_dwn = downconv(h_i, layer_depth, kwidth=kwidth,
                                   init=tf.truncated_normal_initializer(stddev=0.02),
                                   bias_init=bias_init,
                                   name='enc_{}'.format(layer_idx))
                if is_ref:
                    print('Downconv {} -> {}'.format(h_i.get_shape(),
                                                     h_i_dwn.get_shape()))
                h_i = h_i_dwn
                if layer_idx < len(segan.g_enc_depths) - 1:
                    if is_ref:
                        print('Adding skip connection downconv '
                              '{}'.format(layer_idx))
                    # store skip connection
                    # last one is not stored cause it's the code
                    skips.append(h_i)
                if do_prelu:
                    if is_ref:
                        print('-- Enc: prelu activation --')
                    h_i = prelu(h_i, ref=is_ref, name='enc_prelu_{}'.format(layer_idx))
                    if is_ref:
                        # split h_i into its components
                        alpha_i = h_i[1]
                        h_i = h_i[0]
                        alphas.append(alpha_i)
                else:
                    if is_ref:
                        print('-- Enc: leakyrelu activation --')
                    h_i = leakyrelu(h_i)
        with tf.variable_scope("g_gru"):
            zmid = h_i
            encode_z = zmid[:,:,:256]
            h_i,  hidden_state = self.grucell(tf.squeeze(zmid),hidden_state)
            h_i = tf.expand_dims(h_i, [-2])
            z = tf.nn.softmax(h_i[:,:,256:])
            zdim = z.get_shape().as_list()[-1]
            zstack = tf.reshape(z,shape=[segan.batch_size, 1, zdim])
            real_z = h_i[:,:,:256]
            h_i = gaussian_noise_layer(h_i[:,:,:256],1e-2)
            zmid = h_i
            #SECOND DECODER (reverse order)
        with tf.variable_scope("g_d") as scope:
            g_dec_depths = segan.g_enc_depths[:-1][::-1] + [1]
            if is_ref:
                print('g_dec_depths: ', g_dec_depths)
            for layer_idx, layer_depth in enumerate(g_dec_depths):
                h_i_dim = h_i.get_shape().as_list()
                dimension = h_i.get_shape().as_list()[1]
                zconcat = zstack*tf.ones([segan.batch_size, dimension, zdim])
                h_i = tf.concat(values=[h_i, zconcat], axis=2)
                out_shape = [h_i_dim[0], h_i_dim[1] * 2, layer_depth]
                bias_init = None
                # deconv
                if segan.deconv_type == 'deconv':
                    if is_ref:
                        print('-- Transposed deconvolution type --')
                        if segan.bias_deconv:
                            print('Biasing deconv in G')
                    if segan.bias_deconv:
                        bias_init = tf.constant_initializer(0.)
                    h_i_dcv = deconv(h_i, out_shape, kwidth=kwidth, dilation=2,
                                     init=tf.truncated_normal_initializer(stddev=0.02),
                                     bias_init=bias_init,
                                     name='dec_{}'.format(layer_idx))
                elif segan.deconv_type == 'nn_deconv':
                    if is_ref:
                        print('-- NN interpolated deconvolution type --')
                        if segan.bias_deconv:
                            print('Biasing deconv in G')
                    if segan.bias_deconv:
                        bias_init = 0.
                    h_i_dcv = nn_deconv(h_i, kwidth=kwidth, dilation=2,
                                        init=tf.truncated_normal_initializer(stddev=0.02),
                                        bias_init=bias_init,
                                        name='dec_{}'.format(layer_idx))
                else:
                    raise ValueError('Unknown deconv type {}'.format(segan.deconv_type))
                if is_ref:
                    print('Deconv {} -> {}'.format(h_i.get_shape(),
                                                   h_i_dcv.get_shape()))
                h_i = h_i_dcv
                if layer_idx < len(g_dec_depths) - 1:
                    if do_prelu:
                        if is_ref:
                            print('-- Dec: prelu activation --')
                        h_i = prelu(h_i, ref=is_ref,
                                    name='dec_prelu_{}'.format(layer_idx))
                        if is_ref:
                            # split h_i into its components
                            alpha_i = h_i[1]
                            h_i = h_i[0]
                            alphas.append(alpha_i)
                    else:
                        if is_ref:
                            print('-- Dec: leakyrelu activation --')
                        h_i = leakyrelu(h_i)
                    # fuse skip connection
                    skip_ = skips[-(layer_idx + 1)]
                    if is_ref:
                        print('Fusing skip connection of '
                              'shape {}'.format(skip_.get_shape()))
                    h_i = tf.concat(axis=2, values=[h_i, skip_])

                else:
                    if is_ref:
                        print('-- Dec: tanh activation --')
                    h_i = tf.tanh(h_i)

            wave = h_i
            if is_ref and do_prelu:
                print('Amount of alpha vectors: ', len(alphas))
            segan.gen_wave_summ = histogram_summary('gen_wave', wave)
            if is_ref:
                print('Amount of skip connections: ', len(skips))
                print('Last wave shape: ', wave.get_shape())
                print('*************************')
            segan.generator_built = True
            # ret feats contains the features refs to be returned
            ret_feats = [wave]
            ret_feats.append(z)
            ret_feats.append(zmid)
            ret_feats.append(hidden_state)
            ret_feats.append(real_z)
            ret_feats.append(encode_z)
            if is_ref and do_prelu:
                ret_feats += alphas
            return ret_feats


class MultiGenerator(object):
    def __init__(self, segan):
        self.segan = segan
        self.grucell = tf.contrib.rnn.GRUCell(256+self.segan.accent_class)
    def zero(self,batch_size):
        grucell = self.grucell
        return grucell.zero_state(batch_size, tf.float32)
    def __call__(self, noisy_w,hidden_state, is_ref, h_i=None, modus=0,
    	spk=None, z_on=False, do_prelu=False):
        # TODO: remove c_vec
        """ Build the graph propagating (noisy_w) --> x
        On first pass will make variables.
        """
        segan = self.segan
        def make_z(shape, mean=0., std=1., name='z'):
            if is_ref:
                with tf.variable_scope(name) as scope:
                    z_init = tf.random_normal_initializer(mean=mean, stddev=std)
                    z = tf.get_variable("z", shape,
                                        initializer=z_init,
                                        trainable=False
                                        )
                    if z.device != "/device:GPU:0":
                        # this has to be created into gpu0
                        print('z.device is {}'.format(z.device))
                        assert False
            else:
                z = tf.random_normal(shape, mean=mean, stddev=std,
                                     name=name, dtype=tf.float32)
            return z

        if hasattr(segan, 'generator_built'):
            tf.get_variable_scope().reuse_variables()
            make_vars = False
        else:
            make_vars = True
        if is_ref:
            print('*** Building Generator ***')
        in_dims = noisy_w.get_shape().as_list()
        if modus == 0:
        	h_i = noisy_w
        if len(in_dims) == 2:
            h_i = tf.expand_dims(noisy_w, -1)
        elif len(in_dims) < 2 or len(in_dims) > 3:
            raise ValueError('Generator input must be 2-D or 3-D')
        kwidth = 31
        enc_layers = 7
        skips = []
        if is_ref and do_prelu:
            #keep track of prelu activations
            alphas = []
        if modus == 0 :
	        with tf.variable_scope('g_e'):
	            #AE to be built is shaped:
	            # enc ~ [16384x1, 8192x16, 4096x32, 2048x32, 1024x64, 512x64, 256x128, 128x128, 64x256, 32x256, 16x512, 8x1024]
	            # dec ~ [8x2048, 16x1024, 32x512, 64x512, 8x256, 256x256, 512x128, 1024x128, 2048x64, 4096x64, 8192x32, 16384x1]
	            #FIRST ENCODER
	            for layer_idx, layer_depth in enumerate(segan.g_enc_depths):
	                bias_init = None
	                if segan.bias_downconv:
	                    if is_ref:
	                        print('Biasing downconv in G')
	                    bias_init = tf.constant_initializer(0.)
	                h_i_dwn = downconv(h_i, layer_depth, kwidth=kwidth,
	                                   init=tf.truncated_normal_initializer(stddev=0.02),
	                                   bias_init=bias_init,
	                                   name='enc_{}'.format(layer_idx))
	                if is_ref:
	                    print('Downconv {} -> {}'.format(h_i.get_shape(),
	                                                     h_i_dwn.get_shape()))
	                h_i = h_i_dwn
	                if layer_idx < len(segan.g_enc_depths) - 1:
	                    if is_ref:
	                        print('Adding skip connection downconv '
	                              '{}'.format(layer_idx))
	                    # store skip connection
	                    # last one is not stored cause it's the code
	                    skips.append(h_i)
	                if do_prelu:
	                    if is_ref:
	                        print('-- Enc: prelu activation --')
	                    h_i = prelu(h_i, ref=is_ref, name='enc_prelu_{}'.format(layer_idx))
	                    if is_ref:
	                        # split h_i into its components
	                        alpha_i = h_i[1]
	                        h_i = h_i[0]
	                        alphas.append(alpha_i)
	                else:
	                    if is_ref:
	                        print('-- Enc: leakyrelu activation --')
	                    h_i = leakyrelu(h_i)
        with tf.variable_scope("g_gru"):
            zmid = h_i
            encode_z = zmid[:,:,:256]
            if modus != 2:
            	h_i,  hidden_state = self.grucell(tf.squeeze(zmid),hidden_state)
            	h_i = tf.expand_dims(h_i, [-2])
            z = tf.nn.softmax(h_i[:,:,256:])
            zdim = z.get_shape().as_list()[-1]
            zstack = tf.reshape(z,shape=[segan.batch_size, 1, zdim])
            real_z = h_i[:,:,:256]
            h_i = tf.concat([gaussian_noise_layer(h_i[:,:,:256],1e-1), 
            	h_i[:,:,256:]],axis=2)
            zmid = h_i
            h_i = h_i[:,:,:256]
            #SECOND DECODER (reverse order)
        with tf.variable_scope("g_d") as scope:
            g_dec_depths = segan.g_enc_depths[:-1][::-1] + [1]
            if is_ref:
                print('g_dec_depths: ', g_dec_depths)
            for layer_idx, layer_depth in enumerate(g_dec_depths):
                h_i_dim = h_i.get_shape().as_list()
                dimension = h_i.get_shape().as_list()[1]
                zconcat = zstack*tf.ones([segan.batch_size, dimension, zdim])
                out_shape = [h_i_dim[0], h_i_dim[1] * 2, layer_depth]
                h_i = tf.concat(values=[h_i, zconcat], axis=2)
                bias_init = None
                # deconv
                if segan.deconv_type == 'deconv':
                    if is_ref:
                        print('-- Transposed deconvolution type --')
                        if segan.bias_deconv:
                            print('Biasing deconv in G')
                    if segan.bias_deconv:
                        bias_init = tf.constant_initializer(0.)
                    h_i_dcv = deconv(h_i, out_shape, kwidth=kwidth, dilation=2,
                                     init=tf.truncated_normal_initializer(stddev=0.02),
                                     bias_init=bias_init,
                                     name='dec_{}'.format(layer_idx))
                elif segan.deconv_type == 'nn_deconv':
                    if is_ref:
                        print('-- NN interpolated deconvolution type --')
                        if segan.bias_deconv:
                            print('Biasing deconv in G')
                    if segan.bias_deconv:
                        bias_init = 0.
                    h_i_dcv = nn_deconv(h_i, kwidth=kwidth, dilation=2,
                                        init=tf.truncated_normal_initializer(stddev=0.02),
                                        bias_init=bias_init,
                                        name='dec_{}'.format(layer_idx))
                else:
                    raise ValueError('Unknown deconv type {}'.format(segan.deconv_type))
                if is_ref:
                    print('Deconv {} -> {}'.format(h_i.get_shape(),
                                                   h_i_dcv.get_shape()))
                h_i = h_i_dcv
                if layer_idx < len(g_dec_depths) - 1:
                    if do_prelu:
                        if is_ref:
                            print('-- Dec: prelu activation --')
                        h_i = prelu(h_i, ref=is_ref,
                                    name='dec_prelu_{}'.format(layer_idx))
                        if is_ref:
                            # split h_i into its components
                            alpha_i = h_i[1]
                            h_i = h_i[0]
                            alphas.append(alpha_i)
                    else:
                        if is_ref:
                            print('-- Dec: leakyrelu activation --')
                        h_i = leakyrelu(h_i)
                    # fuse skip connection
                    if modus == 0:
                        if not hasattr(self, "skip"):
                            self.skip = {}
                        skip_ = skips[-(layer_idx + 1)]
                        if is_ref:
                            print('Fusing skip connection of '
                                  'shape {}'.format(skip_.get_shape()))
                        h_i = tf.concat(axis=2, values=[h_i, skip_])
                        self.skip["layer_%d"%(layer_idx)] = skip_.get_shape().as_list()
                    else :
                        dimension = h_i.get_shape().as_list()[1]
                        shape = h_i.get_shape().as_list()
                        shape[2] /= 2
                        zconcat = zstack*tf.ones([segan.batch_size, dimension, zdim])
                        t_i = tf.zeros(shape=self.skip["layer_%d"%(layer_idx)])
                        h_i = tf.concat(axis=2, values=[h_i,t_i])
                else:
                    if is_ref:
                        print('-- Dec: tanh activation --')
                    h_i = tf.tanh(h_i)

            wave = h_i
            if is_ref and do_prelu:
                print('Amount of alpha vectors: ', len(alphas))
            segan.gen_wave_summ = histogram_summary('gen_wave', wave)
            if is_ref:
                print('Amount of skip connections: ', len(skips))
                print('Last wave shape: ', wave.get_shape())
                print('*************************')
            segan.generator_built = True
            # ret feats contains the features refs to be returned
            ret_feats = [wave]
            ret_feats.append(z)
            ret_feats.append(zmid)
            ret_feats.append(hidden_state)
            ret_feats.append(real_z)
            ret_feats.append(encode_z)
            if is_ref and do_prelu:
                ret_feats += alphas
            return ret_feats
