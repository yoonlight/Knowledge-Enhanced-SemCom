import os
import sys
import copy

import torch
import torch.utils.data as data
import torch.optim as optim
import numpy as np

from data_loader import WebNLG, collate_func
from preprocess import WebNLGTokenizer
import model
from model import make_model,subsequent_mask,make_std_mask,make_decoder
from utils import Channel, Crit, clip_gradient


os.chdir(sys.path[0])

_iscomplex = True
batch_size = 32
epochs = 100
learning_rate = 1e-4  
epoch_start = 1  # only used when loading ckpt
max_triples = 16

# set path
save_model_path = "./ckpt/"
data_path = 'dataset'

if not os.path.exists(save_model_path): os.makedirs(save_model_path)

use_cuda = torch.cuda.is_available()
device = torch.device("cuda:0" if use_cuda else "cpu")
data_parallel = False

# data loading parameters
train_loader_params = {'batch_size': batch_size,
                       'shuffle': True, 'num_workers':8,
                       'collate_fn': lambda x: collate_func(x),
                       'drop_last': True}

data_train = WebNLG(mode="train")
train_data_loader = data.DataLoader(data_train,**train_loader_params)

vocab_size = data_train.tokenizer.n_words

tmp_model = make_model(vocab_size,vocab_size,act1=False,act2=False).to(device)
tmp_model.load_state_dict(torch.load('ckpt/Transformer_Base.ckpt'))      

c = copy.deepcopy
attn = model.MultiHeadedAttention(8, 128)
ff = model.PositionwiseFeedForward(128, 1024, dropout=0.1)

decoder_new = model.Decoder(model.DecoderLayer(128, c(attn), c(attn), 
                             c(ff), 0.1), 3, 128, False).to(device)
tmp_model.decoder = decoder_new

knowledge_extractor = model.KnowledgeExtractor(embd_dim=128, output_dim=data_train.tokenizer.n_triples, num_layers=3, device=device)
knowledge_extractor.load_state_dict(torch.load('ckpt/Transformer_extractor.ckpt'))
knowledge_extractor.eval()

knowledge_embedding = model.KnowledgeEmbedding(n_embd=data_train.tokenizer.n_triples+1, embd_dim=128, output_dim=128).to(device)
knowledge_embedding.train()

channel = Channel(_iscomplex=_iscomplex)
optimizer = torch.optim.Adam([
    {'params': tmp_model.parameters()},
    {'params': knowledge_embedding.parameters()}],
    lr=learning_rate)
scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones = [10,20,40], gamma = 0.5)
crit = Crit()

TRIPLE_PAD_TOKEN = data_train.tokenizer.n_triples


for epoch in range(epoch_start, epochs + epoch_start):

    print('--------------------epoch: %d' % epoch)

    for batch_idx, (train_sents, train_triples, len_batch) in enumerate(train_data_loader):
        # distribute data to device
        train_sents = train_sents.to(device)  # with eos
        #print(train_sents)############################
        len_batch = len_batch.to(device) #cpu()
        
        src = train_sents[:, 1:]
        trg = train_sents[:, :-1]
        trg_y = train_sents[:, 1:]
        src_mask = (src != 0).unsqueeze(-2).to(device)
        tgt_mask = make_std_mask(trg).to(device)
        _snr1 = np.random.randint(-5, 10)

        with torch.no_grad():
            output= tmp_model.encode(src, src_mask)
            output= channel.agwn(output, _snr=_snr1)
            output= tmp_model.from_channel_emb(output)

            t_pred = knowledge_extractor(output)
            t_pred = (t_pred > 0.5).cpu()
            buf = []
            for b in range(batch_size):
                indices = torch.nonzero(t_pred[b, :]).squeeze(1).tolist()
                while len(indices) < max_triples:
                    indices.append(TRIPLE_PAD_TOKEN)
                indices = indices[:max_triples]
                buf.append(indices)
            t = torch.tensor(buf, dtype=torch.long).to(device)
        
        k = knowledge_embedding(t)
        k_mask = (t != TRIPLE_PAD_TOKEN).unsqueeze(-2).to(device)

        output = torch.cat((output, k), dim=1)
        src_mask = torch.cat((src_mask, k_mask), dim=2)
    
        output= tmp_model.decode(output, src_mask,trg, tgt_mask)
        output= tmp_model.generator.forward(output)
        loss = crit('xe', output, trg_y, len_batch)
        
        optimizer.zero_grad()
        loss.backward()
        clip_gradient(optimizer, 0.1) 
        optimizer.step()
        if batch_idx % 100==0:
           print('[%4d / %4d]    '%(batch_idx, epoch) , '    loss = ', loss.item())

    if epoch % 10 == 0:
        torch.save(tmp_model.state_dict(),
                   os.path.join(save_model_path, 'Transformer_Full.ckpt'))
        torch.save(knowledge_embedding.state_dict(),
                   os.path.join(save_model_path, 'Transformer_Knowledge_Embedding.ckpt'))
        
    scheduler.step()
