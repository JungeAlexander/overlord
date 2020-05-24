import numpy as np

import torch
import torch.nn.functional as F
from torch import nn

from model.utils import he_init


class Generator(nn.Module):

	def __init__(self, config):
		super().__init__()

		self.config = config

		self.from_rgb = nn.Sequential(
			nn.Conv2d(in_channels=3, out_channels=256, kernel_size=3, stride=1, padding=1)
		)

		self.encoder = nn.Sequential(
			ResBlk(dim_in=256, dim_out=512, normalize=True, downsample=True),
			ResBlk(dim_in=512, dim_out=512, normalize=True, downsample=True),
			# ResBlk(dim_in=256, dim_out=256, normalize=True, downsample=True),
			# ResBlk(dim_in=256, dim_out=256, normalize=True, downsample=True),

			ResBlk(dim_in=512, dim_out=512, normalize=True, downsample=False),
			ResBlk(dim_in=512, dim_out=512, normalize=True, downsample=False)
		)

		self.decoder = nn.Sequential(
			AdainResBlk(dim_in=512, dim_out=512, style_dim=config['style_dim'], upsample=False),
			AdainResBlk(dim_in=512, dim_out=512, style_dim=config['style_dim'], upsample=False),

			# AdainResBlk(dim_in=256, dim_out=256, style_dim=config['style_dim'], upsample=True),
			# AdainResBlk(dim_in=256, dim_out=256, style_dim=config['style_dim'], upsample=True),
			AdainResBlk(dim_in=512, dim_out=512, style_dim=config['style_dim'], upsample=True),
			AdainResBlk(dim_in=512, dim_out=256, style_dim=config['style_dim'], upsample=True)
		)

		self.to_rgb = nn.Sequential(
			nn.InstanceNorm2d(num_features=256, affine=True),
			nn.LeakyReLU(negative_slope=0.2),
			nn.Conv2d(in_channels=256, out_channels=3, kernel_size=1, stride=1, padding=0)

			# TODO: tanh?
		)

		self.apply(he_init)

	def forward(self, content_img, style_code):
		x = self.from_rgb(content_img)

		for block in self.encoder:
			x = block(x)

		content_code = x
		if self.training and self.config['content_std'] != 0:
			noise = torch.zeros_like(x)
			noise.normal_(mean=0, std=self.config['content_std'])

			x = x + noise

		if self.training and self.config['style_std'] != 0:
			noise = torch.zeros_like(style_code)
			noise.normal_(mean=0, std=self.config['style_std'])

			style_code = style_code + noise

		for block in self.decoder:
			x = block(x, style_code)

		return {
			'img': self.to_rgb(x),
			'content_code': content_code.reshape(x.shape[0], -1)
		}


class Discriminator(nn.Module):

	def __init__(self, config, max_conv_dim=512):
		super().__init__()

		self.config = config
		img_size = config['img_shape'][0]

		dim_in = 256  #2**14 // img_size
		blocks = []
		blocks += [nn.Conv2d(3, dim_in, 3, 1, 1)]

		repeat_num = int(np.log2(img_size)) - 2
		for _ in range(repeat_num):
			dim_out = min(dim_in*2, max_conv_dim)
			blocks += [ResBlk(dim_in, dim_out, downsample=True)]
			dim_in = dim_out

		blocks += [nn.LeakyReLU(0.2)]
		blocks += [nn.Conv2d(dim_out, dim_out, 4, 1, 0)]
		blocks += [nn.LeakyReLU(0.2)]
		blocks += [nn.Conv2d(dim_out, config['n_classes'], 1, 1, 0)]
		self.main = nn.Sequential(*blocks)

		self.apply(he_init)

	def forward(self, x, y):
		out = self.main(x)
		out = out.view(out.size(0), -1)  # (batch, num_domains)
		idx = torch.LongTensor(range(y.size(0))).to(y.device)
		out = out[idx, y]  # (batch)
		return out


class MappingNetwork(nn.Module):

	def __init__(self, config, latent_dim=16):
		super().__init__()

		self.config = config

		layers = []
		layers += [nn.Linear(latent_dim, 512)]
		layers += [nn.ReLU()]
		for _ in range(3):
			layers += [nn.Linear(512, 512)]
			layers += [nn.ReLU()]
		self.shared = nn.Sequential(*layers)

		self.unshared = nn.ModuleList()
		for _ in range(config['n_classes']):
			self.unshared += [nn.Sequential(nn.Linear(512, 512),
											nn.ReLU(),
											nn.Linear(512, 512),
											nn.ReLU(),
											nn.Linear(512, 512),
											nn.ReLU(),
											nn.Linear(512, config['style_dim']))]

		self.apply(he_init)

	def forward(self, z, y):
		h = self.shared(z)
		out = []
		for layer in self.unshared:
			out += [layer(h)]
		out = torch.stack(out, dim=1)  # (batch, num_domains, style_dim)
		idx = torch.LongTensor(range(y.size(0))).to(y.device)
		s = out[idx, y]  # (batch, style_dim)
		return s


class StyleEncoder(nn.Module):

	def __init__(self, config, max_conv_dim=512):
		super().__init__()

		self.config = config
		img_size = config['img_shape'][0]

		dim_in = 256  #2**14 // img_size
		blocks = []
		blocks += [nn.Conv2d(3, dim_in, 3, 1, 1)]

		repeat_num = int(np.log2(img_size)) - 2
		for _ in range(repeat_num):
			dim_out = min(dim_in*2, max_conv_dim)
			blocks += [ResBlk(dim_in, dim_out, downsample=True)]
			dim_in = dim_out

		blocks += [nn.LeakyReLU(0.2)]
		blocks += [nn.Conv2d(dim_out, dim_out, 4, 1, 0)]
		blocks += [nn.LeakyReLU(0.2)]
		self.shared = nn.Sequential(*blocks)

		self.unshared = nn.ModuleList()
		for _ in range(config['n_classes']):
			self.unshared += [nn.Linear(dim_out, config['style_dim'])]

		self.apply(he_init)

	def forward(self, x, y):
		h = self.shared(x)
		h = h.view(h.size(0), -1)
		out = []
		for layer in self.unshared:
			out += [layer(h)]
		out = torch.stack(out, dim=1)  # (batch, num_domains, style_dim)
		idx = torch.LongTensor(range(y.size(0))).to(y.device)
		s = out[idx, y]  # (batch, style_dim)
		return s


class ResBlk(nn.Module):

	def __init__(self, dim_in, dim_out, actv=nn.LeakyReLU(0.2), normalize=False, downsample=False):
		super().__init__()

		self.actv = actv
		self.normalize = normalize
		self.downsample = downsample
		self.learned_sc = dim_in != dim_out
		self._build_weights(dim_in, dim_out)

	def _build_weights(self, dim_in, dim_out):
		self.conv1 = nn.Conv2d(dim_in, dim_in, 3, 1, 1)
		self.conv2 = nn.Conv2d(dim_in, dim_out, 3, 1, 1)
		if self.normalize:
			self.norm1 = nn.InstanceNorm2d(dim_in, affine=True)
			self.norm2 = nn.InstanceNorm2d(dim_in, affine=True)
		if self.learned_sc:
			self.conv1x1 = nn.Conv2d(dim_in, dim_out, 1, 1, 0, bias=False)

	def _shortcut(self, x):
		if self.learned_sc:
			x = self.conv1x1(x)
		if self.downsample:
			x = F.avg_pool2d(x, 2)
		return x

	def _residual(self, x):
		if self.normalize:
			x = self.norm1(x)
		x = self.actv(x)
		x = self.conv1(x)
		if self.downsample:
			x = F.avg_pool2d(x, 2)
		if self.normalize:
			x = self.norm2(x)
		x = self.actv(x)
		x = self.conv2(x)
		return x

	def forward(self, x):
		x = self._shortcut(x) + self._residual(x)
		return x / np.sqrt(2)  # unit variance


class AdaIN(nn.Module):

	def __init__(self, style_dim, num_features):
		super().__init__()

		self.norm = nn.InstanceNorm2d(num_features, affine=False)
		self.fc = nn.Linear(style_dim, num_features*2)

	def forward(self, x, s):
		h = self.fc(s)
		h = h.view(h.size(0), h.size(1), 1, 1)
		gamma, beta = torch.chunk(h, chunks=2, dim=1)
		return (1 + gamma) * self.norm(x) + beta


class AdainResBlk(nn.Module):

	def __init__(self, dim_in, dim_out, style_dim, actv=nn.LeakyReLU(0.2), upsample=False):
		super().__init__()

		self.actv = actv
		self.upsample = upsample
		self.learned_sc = dim_in != dim_out
		self._build_weights(dim_in, dim_out, style_dim)

	def _build_weights(self, dim_in, dim_out, style_dim=64):
		self.conv1 = nn.Conv2d(dim_in, dim_out, 3, 1, 1)
		self.conv2 = nn.Conv2d(dim_out, dim_out, 3, 1, 1)
		self.norm1 = AdaIN(style_dim, dim_in)
		self.norm2 = AdaIN(style_dim, dim_out)
		if self.learned_sc:
			self.conv1x1 = nn.Conv2d(dim_in, dim_out, 1, 1, 0, bias=False)

	def _shortcut(self, x):
		if self.upsample:
			x = F.interpolate(x, scale_factor=2, mode='nearest')
		if self.learned_sc:
			x = self.conv1x1(x)
		return x

	def _residual(self, x, s):
		x = self.norm1(x, s)
		x = self.actv(x)
		if self.upsample:
			x = F.interpolate(x, scale_factor=2, mode='nearest')
		x = self.conv1(x)
		x = self.norm2(x, s)
		x = self.actv(x)
		x = self.conv2(x)
		return x

	def forward(self, x, s):
		out = self._residual(x, s)
		out = (out + self._shortcut(x)) / np.sqrt(2)
		return out
