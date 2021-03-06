import os
import time
import numpy as np
import tensorflow as tf

from src.utils import deco_print
from src.utils import sharpe
from src.utils import decomposeReturn
from src.utils import UnexplainedVariation
from src.utils import FamaMcBethAlpha

class ModelIPCA_FFN_ensemble:
	def __init__(self, 
				individual_feature_dim, 
				tSize, 
				hidden_dims, 
				nFactor, 
				logdirs, 
				dl, 
				force_var_reuse=False):
		self._logdirs = logdirs
		self._model = ModelIPCA_FFN(individual_feature_dim=individual_feature_dim, 
									tSize=tSize, 
									hidden_dims=hidden_dims, 
									nFactor=nFactor, 
									lr=0.0, 
									dropout=1.0,
									logdir='.', 
									dl=dl, 
									is_train=False,
									force_var_reuse=force_var_reuse)

	def getBeta(self, sess):
		beta_list = []
		for logdir in self._logdirs:
			self._model.setLogdir(logdir)
			self._model.loadSavedModel(sess)
			beta_list.append(self._model.getBeta(sess))
		return np.array(beta_list).mean(axis=0)

	def getFactors(self, sess, calculate_residual=True):
		beta = self.getBeta(sess)
		beta_list = np.split(beta, self._model._splits_np_data)
		F_list = []
		if calculate_residual:
			residual_list = []
		for R_t, beta_t in zip(self._model._R_list_data, beta_list):
			F_t = np.linalg.pinv(beta_t.T.dot(beta_t)).dot(beta_t.T.dot(R_t))
			F_list.append(F_t)
			if calculate_residual:
				residual_list.append(R_t - beta_t.dot(F_t))
		if calculate_residual:
			residual = np.zeros_like(self._model._mask_data, dtype=float)
			residual[self._model._mask_data] = np.squeeze(np.concatenate(residual_list))
			return np.array(F_list), residual
		else:
			return np.array(F_list), None

	def getMarkowitzWeight(self, sess):
		F, _ = self.getFactors(sess, calculate_residual=False)
		w = self._model._Markowitz(F)
		return w

	def getSDFFactor(self, sess, w):
		F, _ = self.getFactors(sess, calculate_residual=False)
		return F.dot(w)

	def calculateStatistics(self, sess, w):
		SR = sharpe(self.getSDFFactor(sess, w))
		_, residual = self.getFactors(sess, calculate_residual=True)
		R = np.zeros_like(self._model._mask_data, dtype=float)
		R[self._model._mask_data] = self._model._R_data
		UV = UnexplainedVariation(R, residual, self._model._mask_data)
		Alpha = FamaMcBethAlpha(residual, self._model._mask_data, weighted=False)
		Alpha_weighted = FamaMcBethAlpha(residual, self._model._mask_data, weighted=True)
		return (SR, UV, Alpha, Alpha_weighted)

class ModelIPCA_FFN:
	def __init__(self, 
				individual_feature_dim, 
				tSize, 
				hidden_dims, 
				nFactor, 
				lr, 
				dropout,
				logdir, 
				dl, 
				is_train=False,
				force_var_reuse=False):
		self._individual_feature_dim = individual_feature_dim
		self._tSize = tSize
		self._hidden_dims = hidden_dims
		self._nFactor = nFactor
		self._lr = lr
		self._dropout = dropout
		self._logdir = logdir
		self._logdir_nFactor = os.path.join(self._logdir, str(self._nFactor))
		self._is_train = is_train
		self._force_var_reuse = force_var_reuse
		
		self._load_data(dl)
		self._build_placeholder()
		with tf.variable_scope('Model_Layer', reuse=self._force_var_reuse):
			self._build_forward_pass_graph()
		if self._is_train:
			self._build_train_op()

	def _load_data(self, dl):
		for _, (I_macro, I, R, mask) in enumerate(dl.iterateOneEpoch(subEpoch=False)):
			self._I_data = I[mask]
			self._R_data = R[mask]
			self._mask_data = mask
			self._splits_data = mask.sum(axis=1)
			self._splits_np_data = self._splits_data.cumsum()[:-1]
			self._R_list_data = np.split(self._R_data, self._splits_np_data)

	def _build_placeholder(self):
		self._I_placeholder = tf.placeholder(dtype=tf.float32, shape=[None, self._individual_feature_dim], name='IndividualFeature')
		self._R_placeholder = tf.placeholder(dtype=tf.float32, shape=[None], name='Return')
		self._F_placeholder = tf.placeholder(dtype=tf.float32, shape=[self._tSize, self._nFactor], name='Factor')
		self._splits_placeholder = tf.placeholder(dtype=tf.int32, shape=[self._tSize], name='Splits')
		self._dropout_placeholder = tf.placeholder_with_default(1.0, shape=[], name='Dropout')

	def _build_forward_pass_graph(self):
		with tf.variable_scope('NN'):
			h_l = self._I_placeholder
			for l in range(len(self._hidden_dims)):
				with tf.variable_scope('Layer_%d' %l):
					h_l = tf.layers.dense(h_l, self._hidden_dims[l], activation=tf.nn.relu)
					h_l = tf.nn.dropout(h_l, self._dropout_placeholder)

		with tf.variable_scope('Output'):
			self._beta = tf.layers.dense(h_l, self._nFactor)

		R_list = tf.split(value=self._R_placeholder, num_or_size_splits=self._splits_placeholder)
		beta_list = tf.split(value=self._beta, num_or_size_splits=self._splits_placeholder)
		F_list = tf.split(value=self._F_placeholder, num_or_size_splits=self._tSize)

		self._loss = 0
		for R_t, beta_t, F_t in zip(R_list, beta_list, F_list):
			R_hat_t = tf.squeeze(tf.matmul(beta_t, F_t, transpose_b=True), axis=1)
			self._loss += tf.reduce_sum(tf.square(R_t - R_hat_t))
		self._loss /= self._tSize

	def _build_train_op(self):
		optimizer = tf.train.AdamOptimizer(self._lr)
		self._train_op = optimizer.minimize(self._loss)

	def getBeta(self, sess):
		feed_dict = {self._I_placeholder:self._I_data,
					self._R_placeholder:self._R_data,
					self._splits_placeholder:self._splits_data,
					self._dropout_placeholder:1.0}
		beta, = sess.run(fetches=[self._beta], feed_dict=feed_dict)
		return beta

	def getMarkowitzWeight(self, sess):
		F, _ = self._step_factor(sess)
		w = self._Markowitz(F)
		return w

	def getFactors(self, sess, calculate_residual=True):
		F, residual_list = self._step_factor(sess, calculate_residual=calculate_residual)
		if calculate_residual:
			residual = np.zeros_like(self._mask_data, dtype=float)
			residual[self._mask_data] = np.squeeze(np.concatenate(residual_list))
			return F, residual
		else:
			return F, None

	def getSDFFactor(self, sess, w):
		F, _ = self._step_factor(sess)
		return F.dot(w)

	def _Markowitz(self, r):
		Sigma = r.T.dot(r) / r.shape[0]
		mu = np.mean(r, axis=0)
		w = np.dot(np.linalg.pinv(Sigma), mu)
		return w

	def _step_factor(self, sess, calculate_residual=False):
		beta = self.getBeta(sess)
		beta_list = np.split(beta, self._splits_np_data)
		F_list = []
		if calculate_residual:
			residual_list = []
		for R_t, beta_t in zip(self._R_list_data, beta_list):
			F_t = np.linalg.pinv(beta_t.T.dot(beta_t)).dot(beta_t.T.dot(R_t))
			F_list.append(F_t)
			if calculate_residual:
				residual_list.append(R_t - beta_t.dot(F_t))
		if calculate_residual:
			return np.array(F_list), residual_list
		else:
			return np.array(F_list), None

	def _step_parameters(self, sess, F_data, maxIter=1024, tol=1e-06, eval_loss=True):
		feed_dict_train = {self._I_placeholder:self._I_data,
						self._R_placeholder:self._R_data,
						self._F_placeholder:F_data,
						self._splits_placeholder:self._splits_data,
						self._dropout_placeholder:self._dropout}
		
		feed_dict_eval = {self._I_placeholder:self._I_data,
						self._R_placeholder:self._R_data,
						self._F_placeholder:F_data,
						self._splits_placeholder:self._splits_data,
						self._dropout_placeholder:1.0}

		if eval_loss:
			old_loss, = sess.run(fetches=[self._loss], feed_dict=feed_dict_eval)
		else:
			old_variables = self.getParameters(sess)

		success = False
		loss_list = []
		error_list = []

		for _ in range(maxIter):
			sess.run(fetches=[self._train_op], feed_dict=feed_dict_train)
			new_loss, = sess.run(fetches=[self._loss], feed_dict=feed_dict_eval)
			loss_list.append(new_loss)

			if eval_loss:
				error = abs(new_loss - old_loss) / max(abs(old_loss), 1e-08)
				error_list.append(error)
				old_loss = new_loss
			else:
				new_variables = self.getParameters(sess)
				error = self._max_norm_difference(old_variables, new_variables)
				error_list.append(error)
				old_variables = new_variables

			if error < tol:
				success = True
				break

		if success:
			deco_print('Converged! ')
		else:
			deco_print('WARNING: Exceed maximum number of iterations! ')

		return loss_list, error_list

	def _max_norm_difference(self, v1_list, v2_list):
		tmp = 0.0
		for v1, v2 in zip(v1_list, v2_list):
			tmp = max(tmp, np.max(np.abs(v1 - v2)))
		return tmp

	def getParameters(self, sess):
		trainable_variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='Model_Layer')
		return sess.run(trainable_variables)

	def evalLoss(self, sess, F_data):
		feed_dict_eval = {self._I_placeholder:self._I_data,
						self._R_placeholder:self._R_data,
						self._F_placeholder:F_data,
						self._splits_placeholder:self._splits_data,
						self._dropout_placeholder:1.0}
		loss, = sess.run(fetches=[self._loss], feed_dict=feed_dict_eval)
		return loss

	def setLogdir(self, new_logdir):
		self._logdir = new_logdir
		self._logdir_nFactor = os.path.join(new_logdir, str(self._nFactor))

	def randomInitialization(self, sess):
		sess.run(tf.global_variables_initializer())
		deco_print('Random initialization')

	def loadSavedModel(self, sess):
		if tf.train.latest_checkpoint(self._logdir_nFactor) is not None:
			saver = tf.train.Saver(max_to_keep=128)
			saver.restore(sess, tf.train.latest_checkpoint(self._logdir_nFactor))
			deco_print('Restored checkpoint')
		else:
			deco_print('WARNING: Checkpoint not found! Use random initialization! ')
			self.randomInitialization(sess)

	def train(self, sess, initial_F=None, numEpoch=128, 
		maxIter=2048, tol=1e-06):
		if initial_F is None:
			F = np.random.randn(self._tSize, self._nFactor)
		else:
			F = initial_F

		saver = tf.train.Saver(max_to_keep=128)
		if os.path.exists(self._logdir_nFactor):
			os.system('rm -rf %s' %self._logdir_nFactor)

		old_loss = self.evalLoss(sess, F)
		best_loss = float('inf')
		loss_epoch_list = [old_loss]

		time_start = time.time()
		for epoch in range(numEpoch):
			deco_print('Doing Epoch %d' %epoch)
			_, _ = self._step_parameters(sess, F_data=F, maxIter=maxIter, tol=tol)
			F, _ = self._step_factor(sess)
			new_loss = self.evalLoss(sess, F)
			loss_epoch_list.append(new_loss)

			if new_loss < best_loss:
				best_loss = new_loss
				deco_print('Saving current best checkpoint')
				saver.save(sess, save_path=os.path.join(self._logdir_nFactor, 'model-best'))
			time_elapse = time.time() - time_start
			time_est = time_elapse / (epoch+1) * numEpoch
			deco_print('Epoch %d Loss: %0.4f' %(epoch, new_loss))
			deco_print('Epoch %d Elapse/Estimate: %0.2fs/%0.2fs' %(epoch, time_elapse, time_est))
			print('\n')

		return loss_epoch_list

	def calculateStatistics(self, sess, w):
		SR = sharpe(self.getSDFFactor(sess, w))
		_, residual = self.getFactors(sess, calculate_residual=True)
		R = np.zeros_like(self._mask_data, dtype=float)
		R[self._mask_data] = self._R_data
		UV = UnexplainedVariation(R, residual, self._mask_data)
		Alpha = FamaMcBethAlpha(residual, self._mask_data, weighted=False)
		Alpha_weighted = FamaMcBethAlpha(residual, self._mask_data, weighted=True)
		return (SR, UV, Alpha, Alpha_weighted)
