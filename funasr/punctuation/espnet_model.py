from typing import Dict
from typing import Optional
from typing import Tuple

import torch
import torch.nn.functional as F
from typeguard import check_argument_types

from funasr.modules.nets_utils import make_pad_mask
from funasr.punctuation.abs_model import AbsPunctuation
from funasr.torch_utils.device_funcs import force_gatherable
from funasr.train.abs_espnet_model import AbsESPnetModel


class ESPnetPunctuationModel(AbsESPnetModel):

    def __init__(self, punc_model: AbsPunctuation, vocab_size: int, ignore_id: int = 0):
        assert check_argument_types()
        super().__init__()
        self.punc_model = punc_model
        self.sos = 1
        self.eos = 2

        # ignore_id may be assumed as 0, shared with CTC-blank symbol for ASR.
        self.ignore_id = ignore_id

    def nll(
        self,
        text: torch.Tensor,
        punc: torch.Tensor,
        text_lengths: torch.Tensor,
        punc_lengths: torch.Tensor,
        max_length: Optional[int] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Compute negative log likelihood(nll)

        Normally, this function is called in batchify_nll.
        Args:
            text: (Batch, Length)
            punc: (Batch, Length)
            text_lengths: (Batch,)
            max_lengths: int
        """
        batch_size = text.size(0)
        # For data parallel
        if max_length is None:
            text = text[:, :text_lengths.max()]
            punc = punc[:, :text_lengths.max()]
        else:
            text = text[:, :max_length]
            punc = punc[:, :max_length]
        # 1. Create a sentence pair like '<sos> w1 w2 w3' and 'w1 w2 w3 <eos>'
        # text: (Batch, Length) -> x, y: (Batch, Length + 1)
        #x = F.pad(text, [1, 0], "constant", self.eos)
        #t = F.pad(text, [0, 1], "constant", self.ignore_id)
        #for i, l in enumerate(text_lengths):
        #    t[i, l] = self.sos
        #x_lengths = text_lengths + 1

        # 2. Forward Language model
        # x: (Batch, Length) -> y: (Batch, Length, NVocab)
        y, _ = self.punc_model(text, text_lengths)

        # 3. Calc negative log likelihood
        # nll: (BxL,)
        if self.training == False:
            _, indices = y.view(-1, y.shape[-1]).topk(1, dim=1)
            from sklearn.metrics import f1_score
            f1_score = f1_score(punc.view(-1).detach().cpu().numpy(),
                                indices.squeeze(-1).detach().cpu().numpy(),
                                average='micro')
            nll = torch.Tensor([f1_score]).repeat(text_lengths.sum())
            return nll, text_lengths
        else:
            nll = F.cross_entropy(y.view(-1, y.shape[-1]), punc.view(-1), reduction="none", ignore_index=self.ignore_id)
        # nll: (BxL,) -> (BxL,)
        if max_length is None:
            nll.masked_fill_(make_pad_mask(text_lengths).to(nll.device).view(-1), 0.0)
        else:
            nll.masked_fill_(
                make_pad_mask(text_lengths, maxlen=max_length + 1).to(nll.device).view(-1),
                0.0,
            )
        # nll: (BxL,) -> (B, L)
        nll = nll.view(batch_size, -1)
        return nll, text_lengths

    def batchify_nll(self,
                     text: torch.Tensor,
                     punc: torch.Tensor,
                     text_lengths: torch.Tensor,
                     punc_lengths: torch.Tensor,
                     batch_size: int = 100) -> Tuple[torch.Tensor, torch.Tensor]:
        """Compute negative log likelihood(nll) from transformer language model

        To avoid OOM, this fuction seperate the input into batches.
        Then call nll for each batch and combine and return results.
        Args:
            text: (Batch, Length)
            punc: (Batch, Length)
            text_lengths: (Batch,)
            batch_size: int, samples each batch contain when computing nll,
                        you may change this to avoid OOM or increase

        """
        total_num = text.size(0)
        if total_num <= batch_size:
            nll, x_lengths = self.nll(text, punc, text_lengths)
        else:
            nlls = []
            x_lengths = []
            max_length = text_lengths.max()

            start_idx = 0
            while True:
                end_idx = min(start_idx + batch_size, total_num)
                batch_text = text[start_idx:end_idx, :]
                batch_punc = punc[start_idx:end_idx, :]
                batch_text_lengths = text_lengths[start_idx:end_idx]
                # batch_nll: [B * T]
                batch_nll, batch_x_lengths = self.nll(batch_text, batch_punc, batch_text_lengths, max_length=max_length)
                nlls.append(batch_nll)
                x_lengths.append(batch_x_lengths)
                start_idx = end_idx
                if start_idx == total_num:
                    break
            nll = torch.cat(nlls)
            x_lengths = torch.cat(x_lengths)
        assert nll.size(0) == total_num
        assert x_lengths.size(0) == total_num
        return nll, x_lengths

    def forward(self, text: torch.Tensor, punc: torch.Tensor, text_lengths: torch.Tensor,
                punc_lengths: torch.Tensor) -> Tuple[torch.Tensor, Dict[str, torch.Tensor], torch.Tensor]:
        nll, y_lengths = self.nll(text, punc, text_lengths, punc_lengths)
        ntokens = y_lengths.sum()
        loss = nll.sum() / ntokens
        stats = dict(loss=loss.detach())

        # force_gatherable: to-device and to-tensor if scalar for DataParallel
        loss, stats, weight = force_gatherable((loss, stats, ntokens), loss.device)
        return loss, stats, weight

    def collect_feats(self, text: torch.Tensor, punc: torch.Tensor,
                      text_lengths: torch.Tensor) -> Dict[str, torch.Tensor]:
        return {}

    def inference(self, text: torch.Tensor, text_lengths: torch.Tensor) -> Tuple[torch.Tensor, None]:
        return self.punc_model(text, text_lengths)
