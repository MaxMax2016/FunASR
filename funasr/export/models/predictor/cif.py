#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import torch
from torch import nn
import logging
import numpy as np


def sequence_mask(lengths, maxlen=None, dtype=torch.float32, device=None):
	if maxlen is None:
		maxlen = lengths.max()
	row_vector = torch.arange(0, maxlen, 1).to(lengths.device)
	matrix = torch.unsqueeze(lengths, dim=-1)
	mask = row_vector < matrix
	mask = mask.detach()
	
	return mask.type(dtype).to(device) if device is not None else mask.type(dtype)

def sequence_mask_scripts(lengths, maxlen:int):
	row_vector = torch.arange(0, maxlen, 1).type(lengths.dtype).to(lengths.device)
	matrix = torch.unsqueeze(lengths, dim=-1)
	mask = row_vector < matrix
	return mask.type(torch.float32).to(lengths.device)

class CifPredictorV2(nn.Module):
	def __init__(self, model):
		super().__init__()
		
		self.pad = model.pad
		self.cif_conv1d = model.cif_conv1d
		self.cif_output = model.cif_output
		self.threshold = model.threshold
		self.smooth_factor = model.smooth_factor
		self.noise_threshold = model.noise_threshold
		self.tail_threshold = model.tail_threshold
	
	def forward(self, hidden: torch.Tensor,
	            mask: torch.Tensor,
	            ):
		h = hidden
		context = h.transpose(1, 2)
		queries = self.pad(context)
		output = torch.relu(self.cif_conv1d(queries))
		output = output.transpose(1, 2)
		
		output = self.cif_output(output)
		alphas = torch.sigmoid(output)
		alphas = torch.nn.functional.relu(alphas * self.smooth_factor - self.noise_threshold)
		mask = mask.transpose(-1, -2).float()
		alphas = alphas * mask
		
		alphas = alphas.squeeze(-1)
		
		token_num = alphas.sum(-1)
		
		acoustic_embeds, cif_peak = cif(hidden, alphas, self.threshold)
		
		return acoustic_embeds, token_num, alphas, cif_peak
	
	def tail_process_fn(self, hidden, alphas, token_num=None, mask=None):
		b, t, d = hidden.size()
		tail_threshold = self.tail_threshold
		
		zeros_t = torch.zeros((b, 1), dtype=torch.float32, device=alphas.device)
		ones_t = torch.ones_like(zeros_t)
		mask_1 = torch.cat([mask, zeros_t], dim=1)
		mask_2 = torch.cat([ones_t, mask], dim=1)
		mask = mask_2 - mask_1
		tail_threshold = mask * tail_threshold
		alphas = torch.cat([alphas, tail_threshold], dim=1)
		
		zeros = torch.zeros((b, 1, d), dtype=hidden.dtype).to(hidden.device)
		hidden = torch.cat([hidden, zeros], dim=1)
		token_num = alphas.sum(dim=-1)
		token_num_floor = torch.floor(token_num)
		
		return hidden, alphas, token_num_floor


# @torch.jit.script
# def cif(hidden, alphas, threshold: float):
# 	batch_size, len_time, hidden_size = hidden.size()
# 	threshold = torch.tensor([threshold], dtype=alphas.dtype).to(alphas.device)
#
# 	# loop varss
# 	integrate = torch.zeros([batch_size], device=hidden.device)
# 	frame = torch.zeros([batch_size, hidden_size], device=hidden.device)
# 	# intermediate vars along time
# 	list_fires = []
# 	list_frames = []
#
# 	for t in range(len_time):
# 		alpha = alphas[:, t]
# 		distribution_completion = torch.ones([batch_size], device=hidden.device) - integrate
#
# 		integrate += alpha
# 		list_fires.append(integrate)
#
# 		fire_place = integrate >= threshold
# 		integrate = torch.where(fire_place,
# 		                        integrate - torch.ones([batch_size], device=hidden.device),
# 		                        integrate)
# 		cur = torch.where(fire_place,
# 		                  distribution_completion,
# 		                  alpha)
# 		remainds = alpha - cur
#
# 		frame += cur[:, None] * hidden[:, t, :]
# 		list_frames.append(frame)
# 		frame = torch.where(fire_place[:, None].repeat(1, hidden_size),
# 		                    remainds[:, None] * hidden[:, t, :],
# 		                    frame)
#
# 	fires = torch.stack(list_fires, 1)
# 	frames = torch.stack(list_frames, 1)
# 	list_ls = []
# 	len_labels = torch.floor(alphas.sum(-1)).int()
# 	max_label_len = len_labels.max()
# 	for b in range(batch_size):
# 		fire = fires[b, :]
# 		l = torch.index_select(frames[b, :, :], 0, torch.nonzero(fire >= threshold).squeeze())
# 		pad_l = torch.zeros([int(max_label_len - l.size(0)), int(hidden_size)], device=hidden.device)
# 		list_ls.append(torch.cat([l, pad_l], 0))
# 	return torch.stack(list_ls, 0), fires


@torch.jit.script
def cif(hidden, alphas, threshold: float):
	batch_size, len_time, hidden_size = hidden.size()
	threshold = torch.tensor([threshold], dtype=alphas.dtype).to(alphas.device)
	
	# loop varss
	integrate = torch.zeros([batch_size], dtype=alphas.dtype, device=hidden.device)
	frame = torch.zeros([batch_size, hidden_size], dtype=hidden.dtype, device=hidden.device)
	# intermediate vars along time
	list_fires = []
	list_frames = []
	
	for t in range(len_time):
		alpha = alphas[:, t]
		distribution_completion = torch.ones([batch_size], dtype=alphas.dtype, device=hidden.device) - integrate
		
		integrate += alpha
		list_fires.append(integrate)
		
		fire_place = integrate >= threshold
		integrate = torch.where(fire_place,
		                        integrate - torch.ones([batch_size], dtype=alphas.dtype, device=hidden.device),
		                        integrate)
		cur = torch.where(fire_place,
		                  distribution_completion,
		                  alpha)
		remainds = alpha - cur
		
		frame += cur[:, None] * hidden[:, t, :]
		list_frames.append(frame)
		frame = torch.where(fire_place[:, None].repeat(1, hidden_size),
		                    remainds[:, None] * hidden[:, t, :],
		                    frame)
	
	fires = torch.stack(list_fires, 1)
	frames = torch.stack(list_frames, 1)

	fire_idxs = fires >= threshold
	frame_fires = torch.zeros_like(hidden)
	max_label_len = frames[0, fire_idxs[0]].size(0)
	for b in range(batch_size):
		frame_fire = frames[b, fire_idxs[b]]
		frame_len = frame_fire.size(0)
		frame_fires[b, :frame_len, :] = frame_fire
	
		if frame_len >= max_label_len:
			max_label_len = frame_len
	frame_fires = frame_fires[:, :max_label_len, :]
	return frame_fires, fires
