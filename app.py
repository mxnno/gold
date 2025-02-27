import os, sys, pdb
import pickle as pkl
import random
import torch
import re
import json
import numpy as np
import time as tm
import csv


from datasets import Dataset


from copy import deepcopy
from numpy.linalg import norm
from scipy.spatial.distance import mahalanobis as mahala_dist
from tqdm import tqdm as progress_bar
from components.models import IntentModel
from assets.static_vars import device, debug_break

from utils.help import set_seed, setup_gpus, check_directories
from utils.process import get_dataloader, process_data
from utils.arguments import solicit_params
from utils.load import load_data, load_tokenizer, load_ontology, load_best_model
from utils.evaluate import has_majority_vote
from utils.augment import embed_by_model, embed_by_paraphrase, embed_by_bow, embed_by_tfidf, embed_by_length

def sample_oos(target_data):
  """ target_data is a dict of 'train', 'dev', and 'test' as keys
  each of the splits contains a list of BaseInstances for training a direct model"""
  in_scope_count = 0
  oos_examples = []
  for example in target_data['train']:
    if example.oos_label == 0:
      in_scope_count += 1
    else:
      oos_examples.append(example)
  print(in_scope_count)
  sample_size = int(round(in_scope_count / 10))
  print(sample_size)
  print(len(oos_examples))
  sample_ids = np.random.choice(len(oos_examples), size=len(oos_examples), replace=False)
  print(f"Sampled {sample_size} OOS targets")
  return oos_examples, sample_ids

def get_speaker(utterance, size=False):
  if utterance.startswith('<agent>'):
    return 8 if size else '<agent>'
  elif utterance.startswith('<customer>'):
    return 11 if size else '<customer>'
  else:
    print(utterance)
    raise ValueError('Speaker not found')

def swap_for_match(args, oos_sample, candidate, tokenizer):
  """ Inputs:
  oos_sample - a BaseInstance with a known OOS label
  candidate - a single text utterance coming from the source_data
  Returns:
  match - a new BaseInstance that is a deepcopy of the original oos_sample
  """
  match = deepcopy(oos_sample)
  chat = match.context

  if isinstance(chat, list): # star and flow
    max_idx = len(chat) // 2 
    position = random.randint(0,max_idx) * 2
    current_utt = chat[position]
    new_utt = get_speaker(current_utt) + " " + candidate
    match.context[position] = new_utt
  else:                      # rostd and clinc
    match.context = [candidate]
  prepared = prepare_match(args, match, tokenizer)
  return prepared

def prepare_match(args, match, tokenizer):
  conversation = ' '.join(match.context)
  embed_data = tokenizer(conversation, padding='max_length', truncation=True, max_length=args.max_len)
  match.embedding = embed_data['input_ids']
  match.segments = embed_data['token_type_ids']
  match.input_mask = embed_data['attention_mask']
  return match

def process_inputs(matches):
  ''' Input: matches is a list of BaseInstances, this already includes embedding inputs
  Returns: single batch of inputs that can be directly fed into a model '''
  input_batch = {
      'input_ids': torch.tensor([f.embedding for f in matches], dtype=torch.long, device=device), 
      'token_type_ids': torch.tensor([f.segments for f in matches], dtype=torch.long, device=device),
      'attention_mask': torch.tensor([f.input_mask for f in matches], dtype=torch.long, device=device)
    }
  return input_batch

def compute_preds(predictions, threshold, mode):
  if mode in ['maxprob', 'odin']:
    # produces results based on the max confidence value of the prediction
    joined = torch.cat(predictions, axis=0)          # num_examples, num_classes
    expo = torch.exp(joined)                         # exponentiation is required due to LogSoftmax
    values, indexes = torch.max(expo, axis=1)
    return values < threshold
  elif mode == 'entropy':
    # produces results based on the prediction entropy which considers the overall distribution
    joined = torch.cat(predictions, axis=0)          # num_examples, num_classes
    expo = torch.exp(joined)                         # exponentiation is required due to LogSoftmax
    entropy_vals = entropy(expo, axis=1)
    return entropy_vals > threshold

def score_outputs(output, task, method, threshold):
  '''Input: the raw model output of batch_size length, 
      a string designating the type of baseline method
  Returns: an array of scores with batch_size length, 
      each score is a binary 0 or 1 indicating OOS'''
  
  if method in ['odin', 'maxprob', 'entropy']:        # score individual output
    prediction = output.detach().cpu()
    preds = compute_preds([prediction], threshold, method)
    votes = preds if isinstance(preds, np.ndarray) else preds.numpy()

  elif method in ['bert_embed', 'mahalanobis', 'gradient']:   # restack into vertical columns
    clusters = torch.load(f'assets/cache/{task}_{method}.pt')
    clusters = clusters.to(device)

    votes = []
    for vector in output:
      distances =  torch.cdist(vector.unsqueeze(0), clusters, p=2)  # 2 is for L2-norm
      min_distance = torch.min(distances).item()  # each distance is a scalar
      # if the min_dist is greater than some threshold, then it is uncertain
      votes.append(min_distance > threshold)

  elif method == 'dropout':   # single_pred is batch_size x num_classes
    predictions = [torch.argmax(pred, axis=1) for pred, _ in output] 
    max_preds = torch.stack(predictions).transpose(0,1)    # batch_size x 3
    votes = [0 if has_majority_vote(max_pred) else 1 for max_pred in max_preds]

  return votes

def merge_features(target_data, augment_data):
  features = {}
  for split, data in target_data.items():
    if split == 'train':
      merged = [x for x in data if x.oos_label == 0]    # Start with the INS_data
      merged.extend(augment_data)                       # Add the OOS_data
      features[split] = merged
    else:
      features[split] = data
  return features

class MatchMaker(object):

  def __init__(self, args, source_data, target_data, tokenizer, model_class):
    self.technique = args.technique
    self.num_matches = args.num_matches
    self.distance = 'cosine'
    self.batch_size = 8
    self.task = args.task

    self.tokenizer = tokenizer
    self.stats = {'skip': 0, 'filter': 0, 'in_scope': 0, 'out_scope': 0, 'round': 0, 'keep': 0}
    self.initialize_data(args, source_data, target_data)
    self.model = load_best_model(args, model_class, device)
    #self.methods = ['odin', 'mahalanobis', 'dropout']
    self.methods = ['odin', 'dropout']

  def initialize_data(self, args, source_data, target_data):
    self.build_storage(*sample_oos(target_data))
    self.source_data = source_data        # holds natural language candidate utterances

    self.augment_data = []                # holds modified BaseInstances were oos_label is set to 1
    for sample_hash, sample in self.target_samples.items():
      self.tracker[sample_hash].append(sample_hash)
      self.augment_data.append(sample)
    self.commence_augmentation(args.source_data)

  def build_storage(self, oos_examples, sample_ids):
    # thresholds discovered when tuning on dev set, will differ by setting
    thresholds = {'star': {'maxprob': 0.43, 'odin': 0.43, 'bert_embed': 8.9, 'dropout': 0.38, 
                            'entropy': 1.9,  'gradient': 10.9, 'mahalanobis': 11.4},
                  'rostd': {'maxprob': 0.99, 'odin': 0.9, 'bert_embed': 3.6, 'dropout': 0.7,
                            'entropy': 1.2,  'gradient': 3.6, 'mahalanobis': 3.6}, 
                  'flow': {'maxprob': 0.99, 'odin': 0.84, 'bert_embed': 7.7, 'dropout': 0.96,
                            'entropy': 0.5,  'gradient': 7.6, 'mahalanobis': 7.4},
                  'clinc': {'maxprob': 0.99, 'odin': 0.9, 'bert_embed': 3.6, 'dropout': 0.7,
                            'entropy': 1.2,  'gradient': 3.6, 'mahalanobis': 3.6}}
    self.thresholds = thresholds[self.task]

    self.target_samples = {}
    self.candidates = {}
    self.matches = {}
    self.tracker = {}

    for idx in sample_ids:
      sample = oos_examples[idx]
      target_utt = sample.context[-1] if isinstance(sample.context, list) else sample.context
      sample_hash = hash(target_utt) % 10**8

      self.target_samples[sample_hash] = sample  # holds BaseInstances of the original OOS samples
      self.candidates[sample_hash] = []   # holds integer indexes of the source_data
      self.matches[sample_hash] = []      # contains BaseInstance matches created after a swap
      self.tracker[sample_hash] = []      # holds 8-digit hashes of source candidates

  def embed_source(self, source, source_type):
    print("Embedding source_data ...")
    if self.technique == 'encoder':
      embeddings, embedder = embed_by_model(source, source_type)
    elif self.technique == 'paraphrase':
      embeddings, embedder = embed_by_paraphrase(source, source_type)
    elif self.technique == 'glove':
      embeddings, embedder = embed_by_bow(source)
    elif self.technique == 'tfidf':
      embeddings, embedder = embed_by_tfidf(source)
    elif self.technique == 'length':
      embeddings, embedder = embed_by_length(source)
    elif self.technique == 'random':
      embeddings = source
      def embedder(samples):
        return samples
      self.distance = 'euclidean'  # reset to prevent further processing 

    self.source_embeds = embeddings
    self.embedder = embedder

    if self.distance == 'cosine':
      self.cosine_norms = [norm(se) for se in embeddings]
    elif self.distance == 'mahalanobis':
      # sample embeddings to reduce dimensionality, making issue tractable
      if len(embeddings) > 10000:
        monte_carlo = [tuple(row) for row in embeddings if random.random() < 0.1]
      else:  # just keep all of them
        monte_carlo = [tuple(row) for row in embeddings]
      # duplicate rows clearly do not change covariance, and should be removed
      uniques = np.unique(monte_carlo, axis=0)
      V = np.cov(uniques.T)
      # add noise to avoid getting a singular matrix
      noised = V + np.random.normal(0,1e-5,V.shape)
      self.VI = np.linalg.inv(noised)
      print("Created inverse covariance matrix")

  def embed_target(self, target):
    print("Embedding target_data ...")
    # pull utterance out from target_samples and embed it
    single_context = list(target.values())[0].context
    if isinstance(single_context, list):
      target_utts = []
      for _, sample in target.items():
          tutt = sample.context[-1]
          skip_size = get_speaker(tutt, size=True)
          target_utts.append(tutt[skip_size:])
      embeddings = self.embedder(target_utts)
    else:
      embeddings = self.embedder([sample.context for _, sample in target.items()])
    self.target_embeds = {h: emb for h, emb in zip(target.keys(), embeddings)}

  def enough_augment_data(self):
    enough = True
    if self.stats['round'] > 101:
      return enough
    
    total = 0
    hash_batch = []
    for sample_hash, hashes in self.tracker.items():
      hash_size = len(hashes)
      total += hash_size
      if hash_size < 10:
        hash_batch.append('0' + str(hash_size))
      else:
        hash_batch.append(str(hash_size))
      
      if len(hashes) < self.num_matches:
        enough = False

    needed = (self.num_matches + 1) * len(self.tracker)
    ratio = round((total / needed) * 100, 2) 

    rnd = self.stats['round']
    if rnd > 0:
      print(f"Round {rnd}: completed {total}/{needed} which is {ratio}%")
    self.stats['round'] += 1
    return enough

  def calculate_distances(self, sample):
    if self.technique in ['paraphrase', 'encoder', 'glove', 'tfidf']:
      # samples are (hidden_dim,)   tfidf=7000, glove=300, encoder=768
      # source_embeds are (num_sources, hidden_dim)
      # distances are (num_sources,)
      if self.distance == 'euclidean':
        # slow_dist = np.sqrt(np.sum((sample - self.source_embeds) ** 2, axis=1)) 4.5x slower
        distances = [norm(sample - se) for se in self.source_embeds]
      elif self.distance == 'cosine':
        # slow_dist = [scipy_dist.cosine(sample, se) for se in self.source_embeds]  # 12x slower
        sample_norm = norm(sample)
        distances = []
        for source_emb, source_norm in zip(self.source_embeds, self.cosine_norms):
          cosine_similarity = np.inner(sample, source_emb)/(sample_norm * source_norm)
          distances.append(1 - cosine_similarity)
      elif self.distance == 'mahalanobis':
        distances = [mahala_dist(sample, se, self.VI) for se in self.source_embeds]

    elif self.technique == 'length':
      distances = [np.absolute(sample - se) for se in self.source_embeds]
    elif self.technique == 'random':
      source_length = len(self.source_data)
      distances = list( range(1, source_length) ) # list of integers from 1 to N
      random.shuffle(distances)

    return distances

  def extract_candidates(self, args, sample_hash):
    remain_idx = self.stats['round'] * self.batch_size  # how many candidates extracted so far
    candidate_slice = self.candidates[sample_hash][remain_idx:remain_idx+self.batch_size]

    for cs_idx in candidate_slice:
      candidate = self.source_data[cs_idx]             # this is natural langauge text
      cand_hash = hash(candidate) % 10**8
      if cand_hash in self.tracker[sample_hash]:
        self.stats['skip'] += 1
      else:
        oos_sample = self.target_samples[sample_hash]  # this is a BaseInstance
        match = swap_for_match(args, oos_sample, candidate, self.tokenizer)
        match.source_hash = cand_hash
        self.matches[sample_hash].append(match)

  def election_day(self, sample_hash):
    ''' Uses a sample_hash to index into a batch_size of matches.  Then uses an ensemble of 
    unsupervised classifiers to vote on the matches.  Matches which fail to recieve enough
    votes are filtered out, whereas matches that do get enough votes become augment_data'''

    dynamic_batch_size = len(self.matches[sample_hash])
    if self.stats['round'] > 100:
      rigged_election = np.ones(dynamic_batch_size) * 3
      # print("the election was rigged")
      return rigged_election
    if dynamic_batch_size < 2:
      return np.ones(dynamic_batch_size)

    input_batch = process_inputs(self.matches[sample_hash])
    votes = np.empty((len(self.methods), dynamic_batch_size))

    for idx, method in enumerate(self.methods):
      if method == 'gradient':
        self.model.eval()
        gradients = [] 
        for j in range(dynamic_batch_size):
          inputs = ['input_ids', 'token_type_ids', 'attention_mask']
          single_input = {i: input_batch[i][j].unsqueeze(0) for i in inputs}
          _, single_loss = self.model(single_input, [], method)
          single_loss.backward()
          for name, params in self.model.named_parameters():
            if name == "encoder.embeddings.word_embeddings.weight":  # (30525, 768)
              gradient = params.grad.detach().cpu().sum(axis=0)     
              gradients.append(gradient)
          self.model.zero_grad()
        output = torch.stack(gradients).to(device)
      elif method == 'dropout':
        with torch.no_grad():
          self.model.train()
          output = [self.model(input_batch, [], method) for _ in range(3)]
      else:
        with torch.no_grad():
          self.model.eval()
          output, _ = self.model(input_batch, [], method)
      
      thresh = self.thresholds[method]
      votes[idx] = score_outputs(output, self.task, method, thresh)

    match_votes = np.sum(votes, axis=0)  # flattens into a batch_size length array
    return match_votes

  def filter_matches(self, votes, sample_hash):  
    for idx, vote in enumerate(votes):
      within_size_limit = len(self.tracker[sample_hash]) < (self.num_matches + 1)
      if vote >= 2 and within_size_limit:
        match = self.matches[sample_hash][idx]
        self.stats['keep'] += 1
        self.augment_data.append(match)
        self.tracker[sample_hash].append(match.source_hash)
      else:
        self.stats['filter'] += 1
    # reset matches
    self.matches[sample_hash] = []

  def commence_augmentation(self, source_type):
    self.embed_source(self.source_data, source_type)
    self.embed_target(self.target_samples)
    sample_size = len(self.target_embeds)

    for sample_hash, sample in progress_bar(self.target_embeds.items(), total=sample_size):
      distances = self.calculate_distances(sample)
      nearest_indexes = np.argpartition(distances, 1024)[:1024]
      min_values = [(ni, distances[ni]) for ni in nearest_indexes]
      min_values.sort(key=lambda x: x[1]) 
      self.candidates[sample_hash] = [mv[0] for mv in min_values]

def augment_features(args, source_data, target_data, augment_path, tokenizer, ontology):
  model_class = IntentModel(args, ontology, tokenizer)
  maker = MatchMaker(args, source_data, target_data, tokenizer, model_class)

  while not maker.enough_augment_data():
    for sample_hash, candidate_hashes in maker.tracker.items():
      if len(candidate_hashes) < (args.num_matches + 1):
        maker.extract_candidates(args, sample_hash)
        votes = maker.election_day(sample_hash)
        maker.filter_matches(votes, sample_hash)

  features = merge_features(target_data, maker.augment_data)
  pkl.dump(features, open(augment_path, 'wb')) 
  print(features)

  train = features['train']
  new_texts = []
  new_labels = []

  counter = 0
  write_to_csv = []

  for baseInstance in train:

    if counter == 0:
      write_to_csv.append([""] + ["text"] + ["intent"])
    else:
      write_to_csv.append([counter] + [baseInstance.context] + [0])
      
    counter += 1


  with open("/content/drive/MyDrive/Masterarbeit/OOD-Methoden/GOLD/ood_data.csv", 'w', encoding='utf-8') as csvw:
      writer = csv.writer(csvw)
      for row in write_to_csv:
        writer.writerow(row)
      csvw.close()

  # ood_dataset = Dataset.from_dict({'text': new_texts, 'intent': new_labels})
  # ood_dataset.to_csv("/content/drive/MyDrive/Masterarbeit/OOD_Methoden/GOLD/ood_data.csv")
  return features

if __name__ == "__main__":
  args = solicit_params()
  args = setup_gpus(args)
  args = check_directories(args)
  set_seed(args)

  raw_data = load_data(args)
  tokenizer = load_tokenizer(args)
  ontology = load_ontology(args)
  augment_features(args, raw_data, tokenizer, ontology)

  

  """
  1. load all source and target data, sample the OOS examples
  2. embed the source and target data using the args.technique method
  3. for each target sample:
        sort the source_data according to the args.distance method
        if this takes a long time, we can perform bucketing first
  4. extract the top X matches for swapping
  5. perform swap to generate candidates
  6. filter with elections to get augmented data
        prevent voter fraud by skipping duplicates
  7. for each target sample that does not have num_matches, perform the process again
      repeat until num_matches count is met.  Rig the election if necessary.
  """
