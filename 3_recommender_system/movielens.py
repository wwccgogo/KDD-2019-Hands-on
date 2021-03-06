import os
import pandas as pd
import numpy as np
import re
import string
from functools import partial

class MovieLens(object):
    split_by_time = None
    
    def read_user_line(self, l):
        id_, age, gender, occupation, zip_ = l.strip().split('|')
        age = np.searchsorted([20, 30, 40, 50, 60], age)   # bin the ages into <20, 20-30, 30-40, ..., >60
        return {'id': int(id_), 'gender': gender, 'age': age, 'occupation': occupation, 'zip': zip_}
    
    def read_product_line(self, l):
        fields = l.strip().split('|')
        id_ = fields[0]
        title = fields[1]
        genres = fields[-19:]

        # extract year
        if re.match(r'.*\([0-9]{4}\)$', title):
            year = title[-5:-1]
            title = title[:-6].strip()
        else:
            year = 0

        data = {'id': int(id_), 'title': title, 'year': year}
        for i, g in enumerate(genres):
            g = int(g)
            data['genre' + str(i)] = (g != 0)
        return data
    
    def read_rating_line(self, l):
        user_id, product_id, rating, timestamp = l.split()
        return {'user_id': int(user_id), 'product_id': int(product_id), 'rating': float(rating), 'timestamp': int(timestamp)}
    
    def __init__(self, directory):
        '''
        directory: path to movielens directory which should have the three
                   files:
                   users.dat
                   products.dat
                   ratings.dat
        '''
        self.directory = directory

        users = []
        products = []
        ratings = []

        # read ratings
        with open(os.path.join(directory, 'ua.base')) as f:
            for l in f:
                rating = self.read_rating_line(l)
                ratings.append(rating)
        with open(os.path.join(directory, 'ua.test')) as f:
            for l in f:
                rating = self.read_rating_line(l)
                ratings.append(rating)
                
        ratings = pd.DataFrame(ratings)
        product_count = ratings['product_id'].value_counts()
        product_count.name = 'product_count'
        ratings = ratings.join(product_count, on='product_id')
        self.ratings = ratings

        # read users - if user feature does not exist, we find all unique user IDs
        # appeared in the rating table and create an empty table from that.
        user_file = os.path.join(directory, 'u.user')
        with open(user_file) as f:
            for l in f:
                users.append(self.read_user_line(l))
        self.users = pd.DataFrame(users).set_index('id').astype('category')

        # read products
        with open(os.path.join(directory, 'u.item'), encoding='latin1') as f:
            for l in f:
                products.append(self.read_product_line(l))
        self.products = (
                pd.DataFrame(products)
                .set_index('id')
                .astype({'year': 'category'}))
        self.genres = self.products.columns[self.products.dtypes == bool]
        
        self.ratings = self.data_split(self.ratings)
        self.users = self.users[self.users.index.isin(self.ratings['user_id'])]
        self.products = self.products[self.products.index.isin(self.ratings['product_id'])]
        
        self.build_graph()
        self.generate_mask()
        self.generate_candidates()
        
        # Construct the training subgraph.
        train_eid = self.g.filter_edges(lambda edges: edges.data['train']).astype('int64')
        self.g_train = self.g.edge_subgraph(train_eid, preserve_nodes=True)
        self.g_train.copy_from_parent()

    # Change this field to 'timestamp' to perform a user-based temporal split of the dataset,
    # where the ratings of each users are ordered by timestamps.  The first 80% of the ordered
    # data falls into the training set, and the middle 10% and last 10% goes to validation and
    # test set respectively.
    # If this field is None, the ratings of each users would be shuffled and splitted in a
    # ratio of 80-10-10.
    split_by_time = None
    def split_user(self, df, filter_counts=0, timestamp=None):
        df_new = df.copy()
        df_new['prob'] = -1

        df_new_sub = (df_new['product_count'] >= filter_counts).to_numpy().nonzero()[0]
        prob = np.linspace(0, 1, df_new_sub.shape[0], endpoint=False)
        if timestamp is not None and timestamp in df_new.columns:
            df_new = df_new.iloc[df_new_sub].sort_values(timestamp)
            df_new['prob'] = prob
            return df_new
        else:
            np.random.shuffle(prob)
            df_new['prob'].iloc[df_new_sub] = prob
            return df_new

    def data_split(self, ratings):
        ratings = ratings.groupby('user_id', group_keys=False).apply(
                partial(self.split_user, filter_counts=10, timestamp=self.split_by_time))
        ratings['train'] = ratings['prob'] <= 0.8
        ratings['valid'] = (ratings['prob'] > 0.8) & (ratings['prob'] <= 0.8)
        ratings['test'] = ratings['prob'] > 0.8
        ratings.drop(['prob'], axis=1, inplace=True)
        return ratings


    # process the features and build the DGL graph
    def build_graph(self):
        import mxnet as mx
        from mxnet import ndarray as nd
        from mxnet import gluon, autograd
        import dgl
        
        user_ids = list(self.users.index)
        product_ids = list(self.products.index)
        user_ids_invmap = {id_: i for i, id_ in enumerate(user_ids)}
        product_ids_invmap = {id_: i for i, id_ in enumerate(product_ids)}
        self.user_ids = user_ids
        self.product_ids = product_ids
        self.user_ids_invmap = user_ids_invmap
        self.product_ids_invmap = product_ids_invmap

        g = dgl.DGLGraph(multigraph=True)
        g.add_nodes(len(user_ids) + len(product_ids))

        # node type
        node_type = nd.zeros(g.number_of_nodes(), dtype='float32')
        node_type[:len(user_ids)] = 1
        g.ndata['type'] = node_type

        # user features
        print('Adding user features...')
        for user_column in self.users.columns:
            udata = nd.zeros(g.number_of_nodes(), dtype='int64')
            # 0 for padding
            udata[:len(user_ids)] = \
                    nd.from_numpy(self.users[user_column].cat.codes.values.astype('int64') + 1)
            g.ndata[user_column] = udata

        # product genre
        print('Adding product features...')
        product_genres = nd.from_numpy(self.products[self.genres].values.copy().astype('float32'))
        g.ndata['genre'] = nd.zeros((g.number_of_nodes(), len(self.genres)))
        g.ndata['genre'][len(user_ids):len(user_ids) + len(product_ids)] = product_genres

        # product year
        if 'year' in self.products.columns:
            g.ndata['year'] = nd.zeros(g.number_of_nodes(), dtype='int64')
            # 0 for padding
            g.ndata['year'][len(user_ids):len(user_ids) + len(product_ids)] = \
                    nd.from_numpy(self.products['year'].cat.codes.values.astype('int64') + 1)

        '''
        # product title
        print('Parsing title...')
        nlp = stanfordnlp.Pipeline(use_gpu=False, processors='tokenize,lemma')
        vocab = set()
        title_words = []
        for t in tqdm.tqdm(self.products['title'].values):
            doc = nlp(t)
            words = set()
            for s in doc.sentences:
                words.update(w.lemma.lower() for w in s.words
                             if not re.fullmatch(r'['+string.punctuation+']+', w.lemma))
            vocab.update(words)
            title_words.append(words)
        vocab = list(vocab)
        vocab_invmap = {w: i for i, w in enumerate(vocab)}
        # bag-of-words
        g.ndata['title'] = nd.zeros((g.number_of_nodes(), len(vocab)))
        for i, tw in enumerate(tqdm.tqdm(title_words)):
            g.ndata['title'][len(user_ids) + i, [vocab_invmap[w] for w in tw]] = 1
        self.vocab = vocab
        self.vocab_invmap = vocab_invmap
        '''
        
        rating_user_vertices = [user_ids_invmap[id_] for id_ in self.ratings['user_id'].values]
        rating_product_vertices = [product_ids_invmap[id_] + len(user_ids)
                                 for id_ in self.ratings['product_id'].values]
        self.rating_user_vertices = rating_user_vertices
        self.rating_product_vertices = rating_product_vertices

        g.add_edges(
                rating_user_vertices,
                rating_product_vertices,
                data={'inv': nd.zeros(self.ratings.shape[0], dtype='int32'),
                    'rating': nd.from_numpy(self.ratings['rating'].values.astype('float32'))})
        g.add_edges(
                rating_product_vertices,
                rating_user_vertices,
                data={'inv': nd.ones(self.ratings.shape[0], dtype='int32'),
                    'rating': nd.from_numpy(self.ratings['rating'].values.astype('float32'))})
        self.g = g
        g.readonly()
        
    # Assign masks of training, validation and test set onto the DGL graph
    # according to the rating table.
    def generate_mask(self):
        from mxnet import ndarray as nd
        valid_tensor = nd.from_numpy(self.ratings['valid'].values.astype('int32'))
        test_tensor = nd.from_numpy(self.ratings['test'].values.astype('int32'))
        train_tensor = nd.from_numpy(self.ratings['train'].values.astype('int32'))
        edge_data = {
                'valid': valid_tensor,
                'test': test_tensor,
                'train': train_tensor,
                }

        self.g.edges[self.rating_user_vertices, self.rating_product_vertices].data.update(edge_data)
        self.g.edges[self.rating_product_vertices, self.rating_user_vertices].data.update(edge_data)
        
    # Generate the list of products for each user in training/validation/test set.
    def generate_candidates(self):
        self.p_train = []
        self.p_valid = []
        self.p_test = []
        for uid in self.user_ids:
            user_ratings = self.ratings[self.ratings['user_id'] == uid]
            self.p_train.append(np.array(
                [self.product_ids_invmap[i] for i in user_ratings[user_ratings['train']]['product_id'].values]))
            self.p_valid.append(np.array(
                [self.product_ids_invmap[i] for i in user_ratings[user_ratings['valid']]['product_id'].values]))
            self.p_test.append(np.array(
                [self.product_ids_invmap[i] for i in user_ratings[user_ratings['test']]['product_id'].values]))
