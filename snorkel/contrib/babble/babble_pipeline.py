import random

from collections import namedtuple

from snorkel.annotations import LabelAnnotator, load_gold_labels
from snorkel.db_helpers import reload_annotator_labels
from snorkel.models import StableLabel

from snorkel.contrib.pipelines import SnorkelPipeline, TRAIN, DEV, TEST

from snorkel.contrib.babble import Babbler, BabbleStream, link_explanation_candidates

class BabblePipeline(SnorkelPipeline):
    
    def load_train_gold(self, annotator_name='gold', config=None):
        # We check if the label already exists, in case this cell was already executed
        for exp in self.explanations:
            if not isinstance(exp.candidate, self.candidate_class):
                continue
                # raise Exception("Candidate linking must be performed before loading train gold.")
            
            context_stable_ids = exp.candidate.get_stable_id()
            query = self.session.query(StableLabel).filter(
                StableLabel.context_stable_ids == context_stable_ids)
            query = query.filter(StableLabel.annotator_name == annotator_name)
            if not query.count():
                self.session.add(StableLabel(
                    context_stable_ids=context_stable_ids,
                    annotator_name=annotator_name,
                    value=exp.label))

        # Commit session and reload annotator labels
        self.session.commit()
        reload_annotator_labels(self.session, self.candidate_class, 
            annotator_name=annotator_name, split=TRAIN, filter_label_split=False)

    def babble(self, mode, explanations, user_lists={}, config=None):
        if config:
            self.config = config
        
        if any(isinstance(exp.candidate, basestring) for exp in explanations):
            print("Linking candidates...")
            babbler_candidate_splits = (self.config['babbler_candidate_split'] if
                isinstance(self.config['babbler_candidate_split'], list) else 
                [self.config['babbler_candidate_split']])
            candidates = []
            for split in babbler_candidate_splits:
                candidates.extend(self.session.query(self.candidate_class).filter(
                    self.candidate_class.split == split).all())
            print("# CANDIDATES: {}".format(len(candidates)))
            explanations = link_explanation_candidates(explanations, candidates)
        
        # Trim number of explanations
        if self.config['max_explanations']:
            random.seed(self.config['seed'])
            if len(explanations) > self.config['max_explanations']:
                explanations = random.sample(explanations, self.config['max_explanations'])
                print("Reduced number of Explanations to {}".format(len(explanations)))
            else:
                print("Since max_explanations > len(explanations), using all {} Explanations".format(
                    len(explanations)))
            if self.config['verbose']:
                for exp in explanations:
                    print(exp)

        print("Calling babbler...")
        self.babbler = Babbler(self.session,
                               mode=mode, 
                               candidate_class=self.candidate_class, 
                               user_lists=user_lists,
                               apply_filters=self.config['apply_filters'])
        

        if self.config['gold_explanations']:
            self.explanations = explanations
            ParseMock = namedtuple('ParseMock', ['semantics'])
            lfs = []
            for exp in explanations:
                lf = self.babbler.semparser.grammar.evaluate(ParseMock(exp.semantics))
                lf.__name__ = exp.name + '_gold'
                lfs.append(lf)
            self.lfs = lfs
        else:
            self.babbler.apply(explanations, 
                    split=self.config['babbler_label_split'], 
                    parallelism=self.config['parallelism'])            
            self.explanations = self.babbler.get_explanations()
            self.lfs = self.babbler.get_lfs()
        
        self.labeler = LabelAnnotator(lfs=self.lfs)

    def set_babbler_matrices(self, babbler, split=None):
        if split == 0 or split is None:
            self.L_train = babbler.get_label_matrix(split=0)
        if split == 1 or split is None:
            self.L_dev   = babbler.get_label_matrix(split=1)
        if split == 2 or split is None:
            self.L_test  = babbler.get_label_matrix(split=2)

    def label(self, config=None, split=None, clear=True):
        if config:
            self.config = config
        if self.config['supervision'] == 'traditional':
            print("In 'traditional' supervision mode...skipping 'label' stage.")
            return
        self.labeler = LabelAnnotator(lfs=self.lfs)
        splits = [split] if split is not None else self.config['splits']
        for split in splits:
            num_candidates = self.session.query(self.candidate_class).filter(self.candidate_class.split == split).count()
            if num_candidates > 0:
                # NOTE: we currently relabel the babbler_split so that 
                # apply_existing on the other splits will use the same key set.
                L = SnorkelPipeline.label(
                    self, 
                    self.labeler, 
                    split, 
                    clear=clear)
                if clear:
                    clear = False
                num_candidates, num_labels = L.shape
                print("Labeled split {}: ({},{}) sparse (nnz = {})\n".format(split, num_candidates, num_labels, L.nnz))
                if self.config['display_accuracies'] and split == DEV:
                    L_gold_dev = load_gold_labels(self.session, annotator_name='gold', split=1)
                    print(L.lf_stats(self.session, labels=L_gold_dev))