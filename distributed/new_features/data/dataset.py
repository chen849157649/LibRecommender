import numpy as np
import pandas as pd
from sklearn.preprocessing import OneHotEncoder, MultiLabelBinarizer
from distributed.new_features.data.data_info import DataInfo
from distributed.new_features.data.transformed import TransformedSet
from distributed.new_features.utils.column_mapping import col_name2index
from distributed.new_features.utils.unique_features import construct_unique_item_feat
import warnings
warnings.filterwarnings("ignore")


class Dataset(object):
    """Base class for loading dataset

    Warning: This class should not be used directly. Use derived class instead.
    """

    sparse_unique_vals = dict()
    user_indices = None
    item_indices = None
#    dense_col = None
#    sparse_col = None
#    multi_sparse_col = None

    @classmethod
    def load_builtin(cls, name="ml-1m") -> pd.DataFrame:
        pass

#    @classmethod
#    def load_from_file(cls, data, kind="pure"):
#        if kind == "pure":
#            return DatasetPure(data)
#        elif kind == "feat":
#            return DatasetFeat(data)
#        else:
#            raise ValueError("data kind must either be 'pure' or 'feat'.")

    @staticmethod
    def _check_col_names(data):
        if not np.all(["user" in data.columns,
                       "item" in data.columns,
                       "label" in data.columns]):
            raise KeyError("data must contain \"user\", \"item\", \"label\" column names")

    @classmethod
    def _check_subclass(cls):
        if not issubclass(cls, Dataset):
            raise NameError("Please use \"DatasetPure\" or \"DatasetFeat\" to call method")

    @classmethod
    def _set_sparse_unique_vals(cls, train_data, sparse_col):
        for col in sparse_col:
            cls.sparse_unique_vals[col] = np.unique(train_data[col])

    @classmethod
    def _get_feature_offset(cls, sparse_col):
        if cls.__name__.lower().endswith("pure"):
            unique_values = [len(cls.sparse_unique_vals[col]) for col in sparse_col]
        elif cls.__name__.lower().endswith("feat"):
            # plus one for unknown value
            unique_values = [len(cls.sparse_unique_vals[col]) + 1 for col in sparse_col]
        return np.cumsum(np.array([0] + unique_values))

    @staticmethod
    def check_unknown(values, uniques):
        diff = list(np.setdiff1d(values, uniques, assume_unique=True))
        mask = np.in1d(values, uniques, invert=True)
        return diff, mask

    @classmethod
    def _sparse_indices(cls, values, unique, mode="train"):
        if mode == "test":
            diff, not_in_mask = cls.check_unknown(values, unique)
            col_indices = np.searchsorted(unique, values)
            col_indices[not_in_mask] = len(unique)
        elif mode == "train":
            col_indices = np.searchsorted(unique, values)
        else:
            raise ValueError("mode must either be \"train\" or \"test\" ")
        return col_indices

    @classmethod
    def _get_sparse_indices_matrix(cls, data, sparse_col, mode="train"):
        n_samples, n_features = len(data), len(sparse_col)
        sparse_indices = np.zeros((n_samples, n_features), dtype=np.int)
        for i, col in enumerate(sparse_col):
            col_values = data[col].to_numpy()
            unique_values = cls.sparse_unique_vals[col]
            if col == "user":
                cls.user_indices = cls._sparse_indices(col_values, unique_values, mode)
            elif col == "item":
                cls.item_indices = cls._sparse_indices(col_values, unique_values, mode)
            sparse_indices[:, i] = cls._sparse_indices(col_values, unique_values, mode)

        feature_offset = cls._get_feature_offset(sparse_col)
        return sparse_indices + feature_offset[:-1]


class DatasetPure(Dataset):

    @classmethod
    def build_trainset(cls, train_data, sparse_col):
        cls._check_subclass()
        cls._check_col_names(train_data)
        cls._set_sparse_unique_vals(train_data, sparse_col)
        train_sparse_indices = cls._get_sparse_indices_matrix(train_data, ["user", "item"], mode="train")
        labels = train_data["label"].to_numpy(dtype=np.float32)
        #    user_indices = train_sparse_indices[:, 0]
        #    n_users = len(np.unique(user_indices))
        #    item_indices = train_sparse_indices[:, 1] - n_users
        return TransformedSet(cls.user_indices, cls.item_indices, labels), DataInfo(...)  ########
"""
    @classmethod
    def build_testset(cls, test_data):
        cls._check_subclass()
        cls._check_col_names(test_data)
        DatasetFeat._check_col_names(test_data)
        test_sparse_indices = cls._get_sparse_indices_matrix(test_data, ["user", "item"], mode="test")
        return TestSet(sparse_indices=test_sparse_indices)

    @classmethod
    def build_train_test(cls, train_data, test_data):
        trainset = cls.build_trainset(train_data)
        testset = cls.build_testset(test_data)
        return trainset, testset
"""


class DatasetFeat(Dataset):
    """A derived class from :class:`Dataset`, used for data that contains features"""

    @classmethod
    def build_trainset(cls, train_data, sparse_col, dense_col=None, user_col=None, item_col=None, neg=False):
        """build trainset from training data.

        Normally, `user` and `item` column will be transformed into sparse indices,
        so `sparse_col` must be provided.

        If you want to do negative sampling afterwards, `user_col` and `item_col`
        also should be provided, otherwise the model doesn't known how to sample.
        In that case, the four kind of column names may overlap.

        Parameters
        ----------
        train_data: `pandas.DataFrame`
            Data must at least contains three columns, i.e. `user`, `item`, `label`.
        sparse_col: list of sparse feature columns names
        dense_col: list of dense feature column names, optional
        user_col: list of user feature column names, optional
        item_col: list of item feature column names, optional
        neg: bool, optional
            Whether to do negative sampling afterwards

        Returns
        -------
        trainset: `TransformedSet` object
            Data object used for training.
        data_info: `DataInfo` object
            Object that contains some useful information for training and predicting
        """

        cls._check_subclass()
        cls._check_col_names(train_data)
        cls._set_sparse_unique_vals(train_data, sparse_col)
        train_sparse_indices = cls._get_sparse_indices_matrix(train_data, sparse_col, mode="train")
        train_dense_values = train_data[dense_col].to_numpy() if dense_col is not None else None
        labels = train_data["label"].to_numpy(dtype=np.float32)
        col_name_mapping = col_name2index(sparse_col, dense_col, user_col, item_col)
        if neg:
            item_sparse_col_indices = list(col_name_mapping["item_sparse_col"].values())
            item_sparse_unique, item_dense_unique = construct_unique_item_feat(
                train_sparse_indices, train_dense_values, item_sparse_col_indices)
        else:
            item_sparse_unique, item_dense_unique = None, None
        return TransformedSet(cls.user_indices, cls.item_indices, labels, train_sparse_indices,
                              train_dense_values), DataInfo(col_name_mapping,
                                                            train_data[["user", "item", "label"]],
                                                            item_sparse_unique, item_dense_unique)
"""
    @classmethod
    def build_testset(cls, test_data, sparse_col, dense_col=None):
        cls._check_subclass()
        DatasetFeat._check_col_names(test_data)
        test_sparse_indices = cls._get_sparse_indices_matrix(test_data, sparse_col, mode="test")
        test_dense_values = test_data[dense_col].to_numpy() if dense_col is not None else None
        return TestSet(test_sparse_indices, test_dense_values)

    @classmethod
    def build_train_test(cls, train_data, test_data, sparse_col, dense_col=None):
        trainset, data_info = cls.build_trainset(train_data, sparse_col, dense_col)
        testset = cls.build_testset(test_data, sparse_col, dense_col)
        return trainset, testset, data_info
"""



