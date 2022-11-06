import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence

from modules.models.general import AttLayer
from modules.models.nrs.rs_base import MindNRSBase
from modules.utils import read_json


class LSTURRSModel(MindNRSBase):
    """
    Implementation of LSTRU model
    Ref: An, Mingxiao et al. “Neural News Recommendation with Long- and Short-term User Representations.” ACL (2019).
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.category_num, self.num_filters = kwargs.get("category_num", 300), kwargs.get("num_filters", 300)
        self.use_category, self.use_sub = kwargs.get("use_category", 0), kwargs.get("use_subcategory", 0)
        self.user_embed_method, self.user_num = kwargs.get("user_embed_method", None), kwargs.get("user_num", 500001)
        padding = (self.window_size - 1) // 2
        assert 2 * padding == self.window_size - 1, "Kernel size must be an odd number"
        self.news_encode_layer = nn.Sequential(
            nn.Conv1d(self.embedding_dim, self.num_filters, self.window_size, padding=padding),
            nn.ReLU(inplace=True)
        )
        self.news_att_layer = AttLayer(self.num_filters, self.attention_hidden_dim)  # output size is [N, num_filters]
        if self.use_category or self.use_sub:
            self.category_embedding = nn.Embedding(self.category_num, self.num_filters)
        input_dim = self.num_filters * 3 if self.use_category and self.use_sub else self.num_filters
        output_dim = self.num_filters
        if self.use_category and self.use_sub:
            output_dim = self.num_filters * 3
        if self.user_embed_method == "init" or self.user_embed_method == "cat":
            uid_path = kwargs.get("uid_path", None)
            if uid_path is None:
                raise ValueError("Must specify user id dictionary path if you want to use user id to initialize GRU")
            uid2index = read_json(uid_path)
            self.user_embedding = nn.Embedding(len(uid2index) + 1, output_dim)  # count from 1
        self.user_encode_layer = nn.GRU(input_dim, output_dim, batch_first=True, bidirectional=False)
        self.user_att_layer = None  # no attentive layer for LSTUR model

    def text_encode(self, input_feat):
        y = self.dropouts(self.embedding_layer(input_feat))
        y = self.news_encode_layer(y.transpose(1, 2)).transpose(1, 2)
        y = self.news_att_layer(self.dropouts(y))[0]
        return y

    def news_encoder(self, input_feat):
        """input_feat: Size is [N * H, S]"""
        if self.use_category or self.use_sub:  # TODO: optimize input format
            news, cat = self.load_news_feat(input_feat, use_category=True)
            input_feat["news"] = news
            news_embed, cat_embed = self.text_encode(input_feat), self.category_embedding(cat)
            y = torch.cat([torch.reshape(cat_embed, (cat_embed.shape[0], -1)), news_embed], dim=1)
        else:
            y = self.text_encode(input_feat)
        return y

    def user_encoder(self, input_feat):
        y, user_ids = input_feat["history_news"], input_feat["uid"]
        packed_y = pack_padded_sequence(y, input_feat["history_length"].cpu(), batch_first=True, enforce_sorted=False)
        if self.user_embed_method == "init":
            user_embed = self.user_embedding(user_ids)
            _, y = self.user_encode_layer(packed_y, user_embed.unsqueeze(dim=0))
            y = y.squeeze(dim=0)
        elif self.user_embed_method == "cat":
            user_embed = self.user_embedding(user_ids)
            _, y = self.user_encode_layer(packed_y)
            y = torch.cat((y.squeeze(dim=0), user_embed), dim=1)
        else:  # default use last hidden output from GRU network
            y = self.user_encode_layer(packed_y)[1].squeeze(dim=0)
        return y