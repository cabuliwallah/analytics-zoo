#
# Copyright 2018 Analytics Zoo Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import hashlib

from pyspark.sql.types import IntegerType, ShortType, LongType, FloatType, DecimalType, \
    DoubleType, ArrayType, DataType, StructType, StringType, StructField
from pyspark.ml import Pipeline
from pyspark.ml.feature import MinMaxScaler
from pyspark.ml.feature import VectorAssembler
from pyspark.sql.functions import col as pyspark_col, concat, udf, array, broadcast, lit
from pyspark.sql import Row
import pyspark.sql.functions as F
from pyspark.sql.window import Window

from zoo.orca import OrcaContext
from zoo.friesian.feature.utils import *

JAVA_INT_MIN = -2147483648
JAVA_INT_MAX = 2147483647


class Table:
    def __init__(self, df):
        self.df = df
        self.__column_names = self.df.schema.names

    @staticmethod
    def _read_parquet(paths):
        if not isinstance(paths, list):
            paths = [paths]
        spark = OrcaContext.get_spark_session()
        df = spark.read.parquet(*paths)
        return df

    @staticmethod
    def _read_json(paths, cols):
        if not isinstance(paths, list):
            paths = [paths]
        spark = OrcaContext.get_spark_session()
        df = spark.read.json(paths)
        if cols:
            if isinstance(cols, list):
                df = df.select(*cols)
            elif isinstance(cols, str):
                df = df.select(cols)
            else:
                raise Exception("cols should be a column name or list of column names")
        return df

    @staticmethod
    def _read_csv(paths, delimiter=",", header=False, names=None, dtype=None):
        if not isinstance(paths, list):
            paths = [paths]
        spark = OrcaContext.get_spark_session()
        df = spark.read.options(header=header, inferSchema=True, delimiter=delimiter).csv(paths)
        columns = df.columns
        if names:
            if not isinstance(names, list):
                names = [names]
            assert len(names) == len(columns), \
                "names should have the same length as the number of columns"
            for i in range(len(names)):
                df = df.withColumnRenamed(columns[i], names[i])
        tbl = Table(df)
        if dtype:
            if isinstance(dtype, dict):
                for col, type in dtype.items():
                    tbl = tbl.cast(col, type)
            elif isinstance(dtype, str):
                tbl = tbl.cast(columns=None, dtype=dtype)
            elif isinstance(dtype, list):
                columns = df.columns
                assert len(dtype) == len(columns), \
                    "dtype should have the same length as the number of columns"
                for i in range(len(columns)):
                    tbl = tbl.cast(columns=columns[i], dtype=dtype[i])
            else:
                raise ValueError("dtype should be str or a list of str or dict")
        return tbl.df

    def _clone(self, df):
        return Table(df)

    def compute(self):
        """
        Trigger computation of the Table.
        """
        compute(self.df)
        return self

    def to_spark_df(self):
        """
        Convert the current Table to a Spark DataFrame.

        :return: The converted Spark DataFrame.
        """
        return self.df

    def size(self):
        """
        Returns the number of rows in this Table.

        :return: The number of rows in the current Table.
        """
        cnt = self.df.count()
        return cnt

    def broadcast(self):
        """
        Marks the Table as small enough for use in broadcast join.
        """
        self.df = broadcast(self.df)

    def select(self, *cols):
        """
        Select specific columns.

        :param cols: str or a list of str that specifies column names. If it is '*',
                     select all the columns.

        :return: A new Table that contains the specified columns.
        """
        # If cols is None, it makes more sense to raise error
        # instead of returning an empty Table.
        if not cols:
            raise ValueError("cols should be str or a list of str, but got None.")
        return self._clone(self.df.select(*cols))

    def drop(self, *cols):
        """
        Returns a new Table that drops the specified column.
        This is a no-op if schema doesn't contain the given column name(s).

        :param cols: str or a list of str that specifies the name of the columns
               to drop.

        :return: A new Table that drops the specified column.
        """
        return self._clone(self.df.drop(*cols))

    def fillna(self, value, columns):
        """
        Replace null values.

        :param value: int, long, float, string, or boolean.
               Value to replace null values with.
        :param columns: list of str, the target columns to be filled. If columns=None and value
               is int, all columns of integer type will be filled. If columns=None and value is
               long, float, str or boolean, all columns will be filled.

        :return: A new Table that replaced the null values with specified value
        """
        if columns and not isinstance(columns, list):
            columns = [columns]
        if columns:
            check_col_exists(self.df, columns)
        if isinstance(value, int) and JAVA_INT_MIN <= value <= JAVA_INT_MAX:
            if columns:
                col_not_int_list = list(filter(lambda x: x[0] in columns and x[1] != "int",
                                               self.df.dtypes))
                if len(col_not_int_list) == 0:
                    return self._clone(fill_na_int(self.df, value, columns))
            else:
                return self._clone(fill_na_int(self.df, value, columns))
        return self._clone(fill_na(self.df, value, columns))

    def dropna(self, columns, how='any', thresh=None):
        """
        Drops the rows containing null values in the specified columns.

        :param columns: str or a list of str that specifies column names. If it is None,
               it will operate on all columns.
        :param how: If `how` is "any", then drop rows containing any null values in `columns`.
               If `how` is "all", then drop rows only if every column in `columns` is null for
               that row.
        :param thresh: int, if specified, drop rows that have less than thresh non-null values.
               Default is None.

        :return: A new Table that drops the rows containing null values in the specified columns.
        """
        return self._clone(self.df.dropna(how, thresh, subset=columns))

    def distinct(self):
        """
        Select the distinct rows of the Table.

        :return: A new Table that only contains distinct rows.
        """
        return self._clone(self.df.distinct())

    def filter(self, condition):
        """
        Filters the rows that satisfy `condition`. For instance, filter("col_1 == 1") will filter
        the rows that has value 1 at column col_1.

        :param condition: str that gives the condition for filtering.

        :return: A new Table with filtered rows.
        """
        return self._clone(self.df.filter(condition))

    def clip(self, columns, min=None, max=None):
        """
        Clips continuous values so that they are within the range [min, max]. For instance, by
        setting the min value to 0, all negative values in columns will be replaced with 0.

        :param columns: str or a list of str, the target columns to be clipped.
        :param min: numeric, the minimum value to clip values to. Values less than this will be
               replaced with this value.
        :param max: numeric, the maximum value to clip values to. Values greater than this will be
               replaced with this value.

        :return: A new Table that replaced the value less than `min` with specified `min` and the
                 value greater than `max` with specified `max`.
        """
        assert min is not None or max is not None, "at least one of min and max should be not None"
        if columns is None:
            raise ValueError("columns should be str or a list of str, but got None.")
        if not isinstance(columns, list):
            columns = [columns]
        check_col_exists(self.df, columns)
        return self._clone(clip(self.df, columns, min, max))

    def log(self, columns, clipping=True):
        """
        Calculates the log of continuous columns.

        :param columns: str or a list of str, the target columns to calculate log.
        :param clipping: boolean, if clipping=True, the negative values in columns will be
               clipped to 0 and `log(x+1)` will be calculated. If clipping=False, `log(x)` will be
               calculated.

        :return: A new Table that replaced value in columns with logged value.
        """
        if columns is None:
            raise ValueError("columns should be str or a list of str, but got None.")
        if not isinstance(columns, list):
            columns = [columns]
        check_col_exists(self.df, columns)
        return self._clone(log_with_clip(self.df, columns, clipping))

    def fill_median(self, columns):
        """
        Replaces null values with the median in the specified numeric columns. Any column to be
        filled should not contain only null values.

        :param columns: str or a list of str that specifies column names. If it is None,
               it will operate on all numeric columns.

        :return: A new Table that replaces null values with the median in the specified numeric
                 columns.
        """
        if columns and not isinstance(columns, list):
            columns = [columns]
        if columns:
            check_col_exists(self.df, columns)
        return self._clone(fill_median(self.df, columns))

    def median(self, columns):
        """
        Returns a new Table that has two columns, `column` and `median`, containing the column
        names and the medians of the specified numeric columns.

        :param columns: str or a list of str that specifies column names. If it is None,
               it will operate on all numeric columns.

        :return: A new Table that contains the medians of the specified columns.
        """
        if columns and not isinstance(columns, list):
            columns = [columns]
        if columns:
            check_col_exists(self.df, columns)
        return self._clone(median(self.df, columns))

    def merge_cols(self, columns, target):
        """
        Merge the target column values as a list to a new column.
        The original columns will be dropped.

        :param columns: a list of str, the target columns to be merged.
        :param target: str, the new column name of the merged column.

        :return: A new Table that replaces columns with a new target column of merged list values.
        """
        assert isinstance(columns, list), "columns must be a list of column names"
        return self._clone(self.df.withColumn(target, array(columns)).drop(*columns))

    def rename(self, columns):
        """
        Rename columns with new column names

        :param columns: dict. Name pairs. For instance, {'old_name1': 'new_name1', 'old_name2':
               'new_name2'}".

        :return: A new Table with new column names.
        """
        assert isinstance(columns, dict), "columns should be a dictionary of {'old_name1': " \
                                          "'new_name1', 'old_name2': 'new_name2'}"
        new_df = self.df
        for old_name, new_name in columns.items():
            new_df = new_df.withColumnRenamed(old_name, new_name)
        return self._clone(new_df)

    def show(self, n=20, truncate=True):
        """
        Prints the first `n` rows to the console.

        :param n: int, the number of rows to show.
        :param truncate: If set to True, truncate strings longer than 20 chars by default.
               If set to a number greater than one, truncates long strings to length `truncate` and
               align cells right.
        """
        self.df.show(n, truncate)

    def get_stats(self, columns, aggr):
        """
        Calculate the statistics of the values over the target column(s).

        :param columns: str or a list of str that specifies the name(s) of the target column(s).
        If columns is None, then the function will return statistics for all numeric columns.
        :param aggr: str or a list of str or dict to specify aggregate functions,
        min/max/avg/sum/count are supported.
        If aggr is a str or a list of str, it contains the name(s) of aggregate function(s).
        If aggr is a dict, the key is the column name, and the value is the aggregate function(s).

        :return: dict, the key is the column name, and the value is aggregate result(s).
        """
        if columns is None:
            columns = [column for column in self.columns if check_column_numeric(self.df, column)]
        if not isinstance(columns, list):
            columns = [columns]
        check_col_exists(self.df, columns)
        stats = {}
        for column in columns:
            if isinstance(aggr, str) or isinstance(aggr, list):
                aggr_strs = aggr
            elif isinstance(aggr, dict):
                if column not in aggr:
                    raise ValueError("aggregate funtion not defined for the column {}.".
                                     format(column))
                aggr_strs = aggr[column]
            else:
                raise ValueError("aggr must have type str or a list or dict.")
            if isinstance(aggr_strs, str):
                aggr_strs = [aggr_strs]
            values = []
            for aggr_str in aggr_strs:
                if aggr_str not in ["min", "max", "avg", "sum", "count"]:
                    raise ValueError("aggregate function must be one of min/max/avg/sum/count, \
                        but got {}.".format(aggr_str))
                values.append(self.df.agg({column: aggr_str}).collect()[0][0])
            stats[column] = values[0] if len(values) == 1 else values
        return stats

    def min(self, columns):
        """
        Returns a new Table that has two columns, `column` and `min`, containing the column
        names and the minimum values of the specified numeric columns.

        :param columns: str or a list of str that specifies column names. If it is None,
               it will operate on all numeric columns.

        :return: A new Table that contains the minimum values of the specified columns.
        """
        data = self.get_stats(columns, "min")
        data = [(column, float(data[column])) for column in data]
        schema = StructType([StructField("column", StringType(), True),
                             StructField("min", FloatType(), True)])
        spark = OrcaContext.get_spark_session()
        return self._clone(spark.createDataFrame(data, schema))

    def max(self, columns):
        """
        Returns a new Table that has two columns, `column` and `max`, containing the column
        names and the maximum values of the specified numeric columns.

        :param columns: str or a list of str that specifies column names. If it is None,
               it will operate on all numeric columns.

        :return: A new Table that contains the maximum values of the specified columns.
        """
        data = self.get_stats(columns, "max")
        data = [(column, float(data[column])) for column in data]
        schema = StructType([StructField("column", StringType(), True),
                             StructField("max", FloatType(), True)])
        spark = OrcaContext.get_spark_session()
        return self._clone(spark.createDataFrame(data, schema))

    def to_list(self, column):
        """
        Convert all values of the target column to a list.
        Only call this if the Table is small enough.

        :param column: str, specifies the name of target column.

        :return: list, contains all values of the target column.
        """
        if not isinstance(column, str):
            raise ValueError("Column must have type str.")
        check_col_exists(self.df, [column])
        return self.df.select(column).rdd.flatMap(lambda x: x).collect()

    def to_dict(self):
        """
        Convert the Table to a dictionary.
        Only call this if the Table is small enough.

        :return: dict, the key is the column name, and the value is the list containing
        all values in the corresponding column.
        """
        rows = [list(row) for row in self.df.collect()]
        result = {}
        for i, column in enumerate(self.columns):
            result[column] = [row[i] for row in rows]
        return result

    def add(self, columns, value=1):
        """
        Increase all of values of the target numeric column(s) by a constant value.

        :param columns: str or a list of str, the target columns to be increased.
        :param value: numeric (int/float/double/short/long), the constant value to be added.

        :return: A new Table with updated numeric values on specified columns.
        """
        if columns is None:
            raise ValueError("Columns should be str or a list of str, but got None")
        if not isinstance(columns, list):
            columns = [columns]
        check_col_exists(self.df, columns)
        new_df = self.df
        for column in columns:
            if new_df.schema[column].dataType not in [IntegerType(), ShortType(),
                                                      LongType(), FloatType(),
                                                      DecimalType(), DoubleType()]:
                raise ValueError("Column type should be numeric, but have type {} \
                    for column {}".format(new_df.schema[column].dataType, column))
            new_df = new_df.withColumn(column, pyspark_col(column) + lit(value))
        return self._clone(new_df)

    @property
    def columns(self):
        """
        Get column names of the Table.

        :return: A list of strings that specify column names.
        """
        return self.__column_names

    def sample(self, fraction, replace=False, seed=None):
        """
        Return a sampled subset of Table.

        :param fraction: float, fraction of rows to generate, should be within the
               range [0, 1].
        :param replace: allow or disallow sampling of the same row more than once.
        :param seed: seed for sampling.

        :return: A new Table with sampled rows.
        """
        return self._clone(self.df.sample(withReplacement=replace, fraction=fraction, seed=seed))

    def ordinal_shuffle_partition(self):
        """
        Shuffle each partition of the Table by adding a random ordinal column for each row and sort
        by this ordinal column within each partition.

        :return: A new Table with shuffled partitions.
        """
        return self._clone(ordinal_shuffle_partition(self.df))

    def write_parquet(self, path, mode="overwrite"):
        """
        Write the Table to Parquet file.

        :param path: str. The path to the Parquet file.
        :param mode: str. One of "append", "overwrite", "error" or "ignore".
               append: Append contents to the existing data.
               overwrite: Overwrite the existing data.
               error: Throw an exception if the data already exists.
               ignore: Silently ignore this operation if data already exists.
        """
        write_parquet(self.df, path, mode)

    def cast(self, columns, dtype):
        """
        Cast columns to the specified type.

        :param columns: str or a list of str that specifies column names.
               If it is None, then cast all of the columns.
        :param dtype: str ("string", "boolean", "int", "long", "short", "float", "double")
               that specifies the data type.

        :return: A new Table that casts all of the specified columns to the specified type.
        """
        if columns is None:
            columns = self.df.columns
        elif not isinstance(columns, list):
            columns = [columns]
            check_col_exists(self.df, columns)
        valid_types = ["str", "string", "bool", "boolean", "int",
                       "integer", "long", "short", "float", "double"]
        if not (isinstance(dtype, str) and (dtype in valid_types)) \
                and not isinstance(dtype, DataType):
            raise ValueError(
                "dtype should be string, boolean, int, long, short, float, double.")
        transform_dict = {"str": "string", "bool": "boolean", "integer": "int"}
        dtype = transform_dict[dtype] if dtype in transform_dict else dtype
        df_cast = self._clone(self.df)
        for i in columns:
            df_cast.df = df_cast.df.withColumn(i, pyspark_col(i).cast(dtype))
        return df_cast

    def append_column(self, name, value):
        """
        Append a column with a constant value to the Table.

        :param name: str, the name of the new column.
        :param value: The constant column value for the new column.

        :return: A new Table with the appended column.
        """
        return self._clone(self.df.withColumn(name, lit(value)))

    def __getattr__(self, name):
        """
        Get the target column of the Table.
        """
        return self.df.__getattr__(name)

    def col(self, name):
        """
        Get the target column of the Table.
        """
        return pyspark_col(name)


class FeatureTable(Table):
    @classmethod
    def read_parquet(cls, paths):
        """
        Loads Parquet files as a FeatureTable.

        :param paths: str or a list of str. The path(s) to Parquet file(s).

        :return: A FeatureTable for recommendation data.
        """
        return cls(Table._read_parquet(paths))

    @classmethod
    def read_json(cls, paths, cols=None):
        return cls(Table._read_json(paths, cols))

    @classmethod
    def read_csv(cls, paths, delimiter=",", header=False, names=None, dtype=None):
        """
        Loads csv files as a FeatureTable.

        :param paths: str or a list of str. The path(s) to csv file(s).
        :param delimiter: str, delimiter to use for parsing the csv file(s). Default is ",".
        :param header: boolean, whether the first line of the csv file(s) will be treated
               as the header for column names. Default is False.
        :param names: str or a list of str, the column names for the csv file(s). You need to
               provide this if the header cannot be inferred. If specified, names should
               have the same length as the number of columns.
        :param dtype: str or a list of str or dict, the column data type(s) for the csv file(s).\
               You may need to provide this if you want to change the default inferred types
               of specified columns.
               If dtype is a str, then all the columns will be cast to the target dtype.
               If dtype is a list of str, then it should have the same length as the number of
               columns and each column will be cast to the corresponding str dtype.
               If dtype is a dict, then the key should be the column name and the value should be
               the str dtype to cast the column to.

        :return: A FeatureTable for recommendation data.
        """
        return cls(Table._read_csv(paths, delimiter, header, names, dtype))

    def encode_string(self, columns, indices):
        """
        Encode columns with provided list of StringIndex.

        :param columns: str or a list of str, the target columns to be encoded.
        :param indices: StringIndex or a list of StringIndex, StringIndexes of target columns.
               The StringIndex should at least have two columns: id and the corresponding
               categorical column.
               Or it can be a dict or a list of dicts. In this case,
               the keys of the dict should be within the categorical column
               and the values are the target ids to be encoded.

        :return: A new FeatureTable which transforms categorical features into unique integer
                 values with provided StringIndexes.
        """
        if not isinstance(columns, list):
            columns = [columns]
        if not isinstance(indices, list):
            indices = [indices]
        assert len(columns) == len(indices)
        if isinstance(indices[0], dict):
            indices = list(map(lambda x: StringIndex.from_dict(x[1], columns[x[0]]),
                               enumerate(indices)))
        data_df = self.df
        for i in range(len(columns)):
            index_tbl = indices[i]
            col_name = columns[i]
            index_tbl.broadcast()
            data_df = data_df.join(index_tbl.df, col_name, how="left") \
                .drop(col_name).withColumnRenamed("id", col_name)
        return FeatureTable(data_df)

    def filter_by_frequency(self, columns, min_freq=2):
        """
        Filter the FeatureTable by the given minimum frequency on the target columns.

        :param columns: str or a list of str, column names which are considered for filtering.
        :param min_freq: int, min frequency. Columns with occurrence below this value
               would be filtered.

        :return: A new FeatureTable with filtered records.
        """
        freq_df = self.df
        if not isinstance(columns, list):
            columns = [columns]
        name_string = ''
        for column in columns:
            name_string = name_string + column + '_'
        filter_col_name = name_string + 'count'
        key = freq_df.groupby(columns).count().withColumnRenamed('count', filter_col_name)
        group = key.filter(key[filter_col_name] >= min_freq).drop(filter_col_name)
        return FeatureTable(group)

    def hash_encode(self, columns, bins, method='md5'):
        """
        Hash encode for categorical column(s).

        :param columns: str or a list of str, the target columns to be encoded.
               For dense features, you need to cut them into discrete intervals beforehand.
        :param bins: int, defines the number of equal-width bins in the range of column(s) values.
        :param method: hashlib supported method, like md5, sha256 etc.

        :return: A new FeatureTable which hash encoded columns.
        """
        hash_df = self.df
        if not isinstance(columns, list):
            columns = [columns]
        for i in range(len(columns)):
            col_name = columns[i]
            hash_str = lambda x: getattr(hashlib, method)(str(x).encode('utf_8')).hexdigest()
            hash_int = udf(lambda x: int(hash_str(x), 16) % bins)
            hash_df = hash_df.withColumn(col_name, hash_int(pyspark_col(col_name)))
        return FeatureTable(hash_df)

    def cross_hash_encode(self, columns, bins, cross_col_name=None):
        """
        Hash encode for cross column(s).

        :param columns: a list of str, the categorical columns to be encoded as cross features.
               For dense features, you need to cut them into discrete intervals beforehand.
        :param bins: int, defined the number of equal-width bins in the range of column(s) values.
        :param cross_col_name: str, the column name for output cross column. Default is None, and
               in this case the default cross column name will be 'crossed_col1_col2'
               for ['col1', 'col2'].

        :return: A new FeatureTable which the target cross column.
        """
        cross_hash_df = self.df
        assert isinstance(columns, list), "columns should be a list of column names"
        assert len(columns) >= 2, "cross_hash_encode should have >= 2 columns"
        if cross_col_name is None:
            cross_string = ''
            for column in columns:
                cross_string = cross_string + '_' + column
            cross_col_name = 'crossed' + cross_string
        cross_hash_df = cross_hash_df.withColumn(cross_col_name, concat(*columns))
        cross_hash_df = FeatureTable(cross_hash_df).hash_encode([cross_col_name], bins)
        return cross_hash_df

    def category_encode(self, columns, freq_limit=None, order_by_freq=False):
        """
        Category encode the given columns.

        :param columns: str or a list of str, target columns to encode from string to index.
        :param freq_limit: int, dict or None. Categories with a count/frequency below freq_limit
               will be omitted from the encoding. Can be represented as either an integer,
               dict. For instance, 15, {'col_4': 10, 'col_5': 2} etc. Default is None,
               and in this case all the categories that appear will be encoded.
        :param order_by_freq: boolean, whether the result StringIndex will assign smaller indices
               to values with more frequencies. Default is False and in this case frequency order
               may not be preserved when assigning indices.

        :return: A tuple of a new FeatureTable which transforms categorical features into unique
                 integer values, and a list of StringIndex for the mapping.
        """
        indices = self.gen_string_idx(columns, freq_limit, order_by_freq)
        return self.encode_string(columns, indices), indices

    def one_hot_encode(self, columns, sizes=None, prefix=None, keep_original_columns=False):
        """
        Convert categorical features into ont hot encodings.
        If the features are string, you should first call category_encode to encode them into
        indices before one hot encoding.
        For each input column, a one hot vector will be created expanding multiple output columns,
        with the value of each one hot column either 0 or 1.
        Note that you may only use one hot encoding on the columns with small dimensions
        for memory concerns.

        For example, for column 'x' with size 5:
        Input:
        |x|
        |1|
        |3|
        |0|
        Output will contain 5 one hot columns:
        |prefix_0|prefix_1|prefix_2|prefix_3|prefix_4|
        |   0    |   1    |   0    |   0    |   0    |
        |   0    |   0    |   0    |   1    |   0    |
        |   1    |   0    |   0    |   0    |   0    |

        :param columns: str or a list of str, the target columns to be encoded.
        :param sizes: int or a list of int, the size(s) of the one hot vectors of the column(s).
               Default is None, and in this case, the sizes will be calculated by the maximum
               value(s) of the columns(s) + 1, namely the one hot vector will cover 0 to the
               maximum value.
               You are recommended to provided the sizes if they are known beforehand. If specified,
               sizes should have the same length as columns.
        :param prefix: str or a list of str, the prefix of the one hot columns for the input
               column(s). Default is None, and in this case, the prefix will be the input
               column names. If specified, prefix should have the same length as columns.
               The one hot columns for each input column will have column names:
               prefix_0, prefix_1, ... , prefix_maximum
        :param keep_original_columns: boolean, whether to keep the original index column(s) before
               the one hot encoding. Default is False, and in this case the original column(s)
               will be replaced by the one hot columns. If True, the one hot columns will be
               appended to each original column.

        :return: A new FeatureTable which transforms categorical indices into one hot encodings.
        """
        if not isinstance(columns, list):
            columns = [columns]
        if sizes:
            if not isinstance(sizes, list):
                sizes = [sizes]
        else:
            # Take the max of the column to make sure all values are within the range.
            # The vector size is 1 + max (i.e. from 0 to max).
            sizes = [self.select(col_name).group_by(agg="max").df.collect()[0][0] + 1
                     for col_name in columns]
        assert len(columns) == len(sizes), "columns and sizes should have the same length"
        if prefix:
            if not isinstance(prefix, list):
                prefix = [prefix]
            assert len(columns) == len(prefix), "columns and prefix should have the same length"
        data_df = self.df

        def one_hot(columns, sizes):
            one_hot_vectors = []
            for i in range(len(sizes)):
                one_hot_vector = [0] * sizes[i]
                one_hot_vector[columns[i]] = 1
                one_hot_vectors.append(one_hot_vector)
            return one_hot_vectors

        one_hot_udf = udf(lambda columns: one_hot(columns, sizes),
                          ArrayType(ArrayType(IntegerType())))
        data_df = data_df.withColumn("friesian_onehot", one_hot_udf(array(columns)))

        all_columns = data_df.columns
        for i in range(len(columns)):
            col_name = columns[i]
            col_idx = all_columns.index(col_name)
            cols_before = all_columns[:col_idx]
            cols_after = all_columns[col_idx + 1:]
            one_hot_prefix = prefix[i] if prefix else col_name
            one_hot_cols = []
            for j in range(sizes[i]):
                one_hot_col = one_hot_prefix + "_{}".format(j)
                one_hot_cols.append(one_hot_col)
                data_df = data_df.withColumn(one_hot_col,
                                             data_df.friesian_onehot[i][j])
            if keep_original_columns:
                all_columns = cols_before + [col_name] + one_hot_cols + cols_after
            else:
                all_columns = cols_before + one_hot_cols + cols_after
            data_df = data_df.select(*all_columns)
        data_df = data_df.drop("friesian_onehot")
        return FeatureTable(data_df)

    def gen_string_idx(self, columns, freq_limit=None, order_by_freq=False):
        """
        Generate unique index value of categorical features. The resulting index would
        start from 1 with 0 reserved for unknown features.

        :param columns: str or a list of str, target columns to generate StringIndex.
        :param freq_limit: int, dict or None. Categories with a count/frequency below freq_limit
               will be omitted from the encoding. Can be represented as either an integer,
               dict. For instance, 15, {'col_4': 10, 'col_5': 2} etc. Default is None,
               and in this case all the categories that appear will be encoded.
        :param order_by_freq: boolean, whether the result StringIndex will assign smaller indices
               to values with more frequencies. Default is False and in this case frequency order
               may not be preserved when assigning indices.

        :return: A StringIndex or a list of StringIndex.
        """
        if columns is None:
            raise ValueError("columns should be str or a list of str, but got None.")
        columns_is_str = False
        if not isinstance(columns, list):
            columns_is_str = True
            columns = [columns]
        check_col_exists(self.df, columns)
        if freq_limit:
            if isinstance(freq_limit, int):
                freq_limit = str(freq_limit)
            elif isinstance(freq_limit, dict):
                freq_limit = ",".join(str(k) + ":" + str(v) for k, v in freq_limit.items())
            else:
                raise ValueError("freq_limit only supports int, dict or None, but get " +
                                 freq_limit.__class__.__name__)
        df_id_list = generate_string_idx(self.df, columns, freq_limit, order_by_freq)
        string_idx_list = list(map(lambda x: StringIndex(x[0], x[1]),
                                   zip(df_id_list, columns)))
        # If input is a single column (not a list), then the output would be a single StringIndex.
        if len(string_idx_list) == 1 and columns_is_str:
            return string_idx_list[0]
        else:
            return string_idx_list

    def _clone(self, df):
        return FeatureTable(df)

    def cross_columns(self, crossed_columns, bucket_sizes):
        """
        Cross columns and hashed to specified bucket size

        :param crossed_columns: list of column name pairs to be crossed.
               i.e. [['a', 'b'], ['c', 'd']]
        :param bucket_sizes: hash bucket size for crossed pairs. i.e. [1000, 300]

        :return: A new FeatureTable with crossed columns.
        """
        df = cross_columns(self.df, crossed_columns, bucket_sizes)
        return FeatureTable(df)

    def min_max_scale(self, columns, min=0.0, max=1.0):
        """
        Rescale each column individually to a common range [min, max] linearly using
         column summary statistics, which is also known as min-max normalization or Rescaling.

        :param columns: list of column names
        :param min: Lower bound after transformation, shared by all columns. 0.0 by default.
        :param max: Upper bound after transformation, shared by all columns. 1.0 by default.

        :return: FeatureTable and mapping = {c: (originalMin, originalMax) for c in columns}
        """
        df = self.df
        types = [x[1] for x in self.df.select(*columns).dtypes]
        scalar_cols = [columns[i] for i in range(len(columns))
                       if types[i] == "int" or types[i] == "bigint"
                       or types[i] == "float" or types[i] == "double"]
        array_cols = [columns[i] for i in range(len(columns))
                      if types[i] == "array<int>" or types[i] == "array<bigint>"
                      or types[i] == "array<float>" or types[i] == "array<double>"]
        vector_cols = [columns[i] for i in range(len(columns)) if types[i] == "vector"]

        min_max_dic = {}
        tolist = udf(lambda x: x.toArray().tolist(), ArrayType(DoubleType()))

        if scalar_cols:
            assembler = VectorAssembler(inputCols=scalar_cols, outputCol="vect")

            # MinMaxScaler Transformation
            scaler = MinMaxScaler(min=min, max=max, inputCol="vect", outputCol="scaled")

            # Pipeline of VectorAssembler and MinMaxScaler
            pipeline = Pipeline(stages=[assembler, scaler])

            # Fitting pipeline on dataframe
            model = pipeline.fit(df)
            df = model.transform(df) \
                .withColumn("scaled_list", tolist(pyspark_col("scaled"))) \
                .drop("vect").drop("scaled")
            # TODO: Save model.stages[1].originalMax/originalMin as mapping for inference
            for i in range(len(scalar_cols)):
                df = df.withColumn(scalar_cols[i], pyspark_col("scaled_list")[i])
            df = df.drop("scaled_list")

            # cast to float
            for c in scalar_cols:
                df = df.withColumn(c, pyspark_col(c).cast("float"))

            min_list = model.stages[1].originalMin.toArray().tolist()
            max_list = model.stages[1].originalMax.toArray().tolist()

            for i, min_max in enumerate(zip(min_list, max_list)):
                min_max_dic[scalar_cols[i]] = min_max

        from pyspark.ml.linalg import Vectors, VectorUDT
        for c in array_cols:
            list_to_vector_udf = udf(lambda l: Vectors.dense(l), VectorUDT())
            df = df.withColumn(c, list_to_vector_udf(pyspark_col(c)))
            scaler = MinMaxScaler(min=min, max=max, inputCol=c, outputCol="scaled")
            model = scaler.fit(df)
            df = model.transform(df).drop(c).withColumn(c, tolist("scaled")).drop("scaled")
            min_max_dic[c] = (model.originalMin.toArray().tolist(),
                              model.originalMax.toArray().tolist())

        for c in vector_cols:
            scaler = MinMaxScaler(min=min, max=max, inputCol=c, outputCol="scaled")
            model = scaler.fit(df)
            df = model.transform(df).withColumnRenamed("scaled", c)
            min = model.originalMin
            max = model.originalMax
            min_max_dic[c] = (min, max)

        return FeatureTable(df), min_max_dic

    def add_negative_samples(self, item_size, item_col="item", label_col="label", neg_num=1):
        """
        Generate negative item visits for each positive item visit

        :param item_size: integer, max of item.
        :param item_col:  string, name of item column
        :param label_col:  string, name of label column
        :param neg_num:  integer, for each positive record, add neg_num of negative samples

        :return: FeatureTable
        """
        df = add_negative_samples(self.df, item_size, item_col, label_col, neg_num)
        return FeatureTable(df)

    def add_hist_seq(self, cols, user_col, sort_col='time', min_len=1, max_len=100):
        """
        Generate a list of item visits in history

        :param cols:  list of string, ctolumns need to be aggragated
        :param user_col: string, user column.
        :param sort_col:  string, sort by sort_col
        :param min_len:  int, minimal length of a history list
        :param max_len:  int, maximal length of a history list

        :return: FeatureTable
        """
        df = add_hist_seq(self.df, cols, user_col, sort_col, min_len, max_len)
        return FeatureTable(df)

    def add_neg_hist_seq(self, item_size, item_history_col, neg_num):
        """
        Generate a list negative samples for each item in item_history_col

        :param item_size: int, max of item.
        :param item2cat:  FeatureTable with a dataframe of item to catgory mapping
        :param item_history_col:  string, this column should be a list of visits in history
        :param neg_num:  int, for each positive record, add neg_num of negative samples

        :return: FeatureTable
        """
        df = add_neg_hist_seq(self.df, item_size, item_history_col, neg_num)
        return FeatureTable(df)

    def mask(self, mask_cols, seq_len=100):
        """
        Mask mask_cols columns

        :param mask_cols: list of string, columns need to be masked with 1s and 0s.
        :param seq_len:  int, length of masked column

        :return: FeatureTable
        """
        df = mask(self.df, mask_cols, seq_len)
        return FeatureTable(df)

    def pad(self, cols, seq_len=100, mask_cols=None):
        """
        Pad and mask columns of the FeatureTable.

        :param cols: list of str, columns need to be padded with 0s.
        :param seq_len: int, the length of masked column. Default is 100.
        :param mask_cols: list of string, columns need to be masked with 1s and 0s.

        :return: A new FeatureTable with padded columns.
        """
        df = pad(self.df, cols, seq_len, mask_cols)
        return FeatureTable(df)

    def apply(self, in_col, out_col, func, dtype="string"):
        """
        Transform a FeatureTable using a user-defined Python function.

        :param in_col: str, the name of column to be transformed.
        :param out_col: str, the name of output column.
        :param func: The Python function to convert in_col to out_col.
        :param dtype: str, the data type of out_col. Default is string type.

        :return: A new FeatureTable after column transformation.
        """
        udf_func = udf(func, dtype)
        df = self.df.withColumn(out_col, udf_func(pyspark_col(in_col)))
        return FeatureTable(df)

    def join(self, table, on=None, how=None):
        """
        Join a FeatureTable with another FeatureTable, it is wrapper of spark dataframe join

        :param table: FeatureTable
        :param on: string, join on this column
        :param how: string

        :return: FeatureTable
        """
        assert isinstance(table, Table), "the joined table should be a Table"
        joined_df = self.df.join(table.df, on=on, how=how)
        return FeatureTable(joined_df)

    def add_value_features(self, key_cols, tbl, key, value):
        """
         Add features based on key_cols and another key value table,
         for each col in key_cols, it adds a value_col using key-value pairs from tbl

         :param key_cols: list[string]
         :param tbl: Table with only two columns [key, value]
         :param key: string, name of key column in tbl
         :param value: string, name of value column in tbl

         :return: FeatureTable
         """
        spark = OrcaContext.get_spark_session()
        keyvalue_bc = spark.sparkContext.broadcast(dict(tbl.df.distinct().rdd.map(
            lambda row: (row[0], row[1])).collect()))

        keyvalue_map = keyvalue_bc.value

        def gen_values(items):
            getvalue = lambda item: keyvalue_map.get(item)
            if isinstance(items, int):
                values = getvalue(items)
            elif isinstance(items, list) and isinstance(items[0], int):
                values = [getvalue(item) for item in items]
            elif isinstance(items, list) and isinstance(items[0], list) and isinstance(items[0][0],
                                                                                       int):
                values = []
                for line in items:
                    line_cats = [getvalue(item) for item in line]
                    values.append(line_cats)
            else:
                raise ValueError('only int, list[int], and list[list[int]] are supported.')
            return values

        df = self.df
        for c in key_cols:
            col_type = df.schema[c].dataType
            cat_udf = udf(gen_values, col_type)
            df = df.withColumn(c.replace("item", "category"), cat_udf(pyspark_col(c)))
        return FeatureTable(df)

    def group_by(self, columns=[], agg="count", join=False):
        """
        Group the Table with specified columns and then run aggregation. Optionally join the result
        with the original Table.

        :param columns: str or a list of str. Columns to group the Table. If it is an empty list,
               aggregation is run directly without grouping. Default is [].
        :param agg: str, list or dict. Aggregate functions to be applied to grouped Table.
               Default is "count".
               Supported aggregate functions are: "max", "min", "count", "sum", "avg", "mean",
               "sumDistinct", "stddev", "stddev_pop", "variance", "var_pop", "skewness", "kurtosis",
               "collect_list", "collect_set", "approx_count_distinct", "first", "last".
               If agg is a str, then agg is the aggregate function and the aggregation is performed
               on all columns that are not in `columns`.
               If agg is a list of str, then agg is a list of aggregate function and the aggregation
               is performed on all columns that are not in `columns`.
               If agg is a single dict mapping from str to str, then the key is the column
               to perform aggregation on, and the value is the aggregate function.
               If agg is a single dict mapping from str to list, then the key is the
               column to perform aggregation on, and the value is list of aggregate functions.

               Examples:
               agg="sum"
               agg=["last", "stddev"]
               agg={"*":"count"}
               agg={"col_1":"sum", "col_2":["count", "mean"]}
        :param join: boolean. If join is True, join the aggregation result with original Table.

        :return: A new Table with aggregated column fields.
        """
        if isinstance(columns, str):
            columns = [columns]
        assert isinstance(columns, list), "columns should be str or a list of str"
        grouped_data = self.df.groupBy(columns)

        if isinstance(agg, str):
            agg_exprs_dict = {agg_column: agg for agg_column in self.df.columns
                              if agg_column not in columns}
            agg_df = grouped_data.agg(agg_exprs_dict)
        elif isinstance(agg, list):
            agg_exprs_list = []
            for stat in agg:
                stat_func = getattr(F, stat)
                agg_exprs_list += [stat_func(agg_column) for agg_column in self.df.columns
                                   if agg_column not in columns]
            agg_df = grouped_data.agg(*agg_exprs_list)
        elif isinstance(agg, dict):
            if all(isinstance(stats, str) for agg_column, stats in agg.items()):
                agg_df = grouped_data.agg(agg)
            else:
                agg_exprs_list = []
                for agg_column, stats in agg.items():
                    if isinstance(stats, str):
                        stats = [stats]
                    assert isinstance(stats, list), "value in agg should be str or a list of str"
                    for stat in stats:
                        stat_func = getattr(F, stat)
                        agg_exprs_list += [stat_func(agg_column)]
                agg_df = grouped_data.agg(*agg_exprs_list)
        else:
            raise TypeError("agg should be str, list of str, or dict")

        if join:
            assert columns, "columns can not be empty if join is True"
            result_df = self.df.join(agg_df, on=columns, how="left")
            return FeatureTable(result_df)
        else:
            return FeatureTable(agg_df)

    def split(self, ratio, seed=None):
        """
        Split the FeatureTable into multiple FeatureTables for train, validation and test.

        :param ratio: a list of portions as weights with which to split the FeatureTable.
                      Weights will be normalized if they don't sum up to 1.0.
        :param seed: The seed for sampling.

        :return: A tuple of FeatureTables split by the given ratio.
        """
        df_list = self.df.randomSplit(ratio, seed)
        tbl_list = [FeatureTable(df) for df in df_list]
        return tuple(tbl_list)

    def target_encode(self, cat_cols, target_cols, target_mean=None, smooth=20, kfold=2,
                      fold_seed=None, fold_col="__fold__", drop_cat=False, drop_fold=True,
                      out_cols=None):
        """
        For each categorical column or column group in cat_cols, calculate the mean of target
        columns in target_cols and encode the FeatureTable with the target mean(s) to generate
        new features.

        :param cat_cols: str, a list of str or a nested list of str. Categorical column(s) or column
               group(s) to target encode. To encode categorical column(s), cat_cols should be a str
               or a list of str. To encode categorical column group(s), cat_cols should be a nested
               list of str.
        :param target_cols: str or a list of str. Numeric target column(s) to calculate the mean.
               If target_cols is a list, then each target_col would be used separately to encode the
               cat_cols.
        :param target_mean: dict of {target column : mean} to provides global mean of target
               column(s) if known beforehand to save calculation. Default is None and in this case
               the global mean(s) would be calculated on demand.
        :param smooth: int. The mean of each category is smoothed by the overall mean. Default is
               20.
        :param kfold: int. Specifies number of folds for cross validation. The mean values within
               the i-th fold are calculated with data from all other folds. If kfold is 1,
               global-mean statistics are applied; otherwise, cross validation is applied. Default
               is 2.
        :param fold_seed: int. Random seed used for generating folds. Default is None and in this
               case folds will be generated with row number in each partition.
        :param fold_col: str. Name of integer column used for splitting folds. If fold_col exists
               in the FeatureTable, then this column is used; otherwise, it is randomly generated
               within the range [0, kfold). Default is "__fold__".
        :param drop_cat: boolean. Whether to drop the original categorical columns.
               Default is False.
        :param drop_fold: boolean. Drop the fold column if it is true. Default is True.
        :param out_cols: str, a list of str or a nested list of str. When both cat_cols and
               target_cols has only one element, out_cols can be a single str. When cat_cols or
               target_cols has only one element, out_cols can be a list of str, and each element
               in out_cols corresponds to an element in target_cols or cat_cols. When it is a
               nested list of str, each inner list corresponds to the categorical column in the
               same position of cat_cols. Each element in the inner list corresponds to the target
               column in the same position of target_cols. Default to be None and in this case the
               output column will be cat_col + "_te_" + target_col.

        :return: A tuple of a new FeatureTable with target encoded columns and a list of TargetCodes
                 which contains the target encode values of the whole FeatureTable.
        """
        assert isinstance(kfold, int) and kfold > 0, "kfold should be an integer larger than 0"
        if isinstance(cat_cols, str):
            cat_cols = [cat_cols]
        assert isinstance(cat_cols, list), "cat_cols should be str or list"
        for cat_col in cat_cols:
            check_col_str_list_exists(self.df, cat_col, "cat_cols")
        if isinstance(target_cols, str):
            target_cols = [target_cols]
        assert isinstance(target_cols, list), "target_cols should be str or list"
        check_col_exists(self.df, target_cols)
        nonnumeric_target_col_type = get_nonnumeric_col_type(self.df, target_cols)
        assert not nonnumeric_target_col_type, "target_cols should be numeric but get " + ", ".join(
            list(map(lambda x: x[0] + " of type " + x[1], nonnumeric_target_col_type)))

        if out_cols is None:
            out_cols = [[gen_cols_name(cat_col, "_") + "_te_" + target_col
                         for target_col in target_cols] for cat_col in cat_cols]
        else:
            if isinstance(out_cols, str):
                assert len(cat_cols) == 1 and len(target_cols) == 1, \
                    "out_cols can be string only if both cat_cols and target_cols has only one" + \
                    " element"
                out_cols = [[out_cols]]
            elif isinstance(out_cols, list):
                if all(isinstance(out_col, str) for out_col in out_cols):
                    if len(cat_cols) == 1:
                        out_cols = [out_cols]
                    elif len(target_cols) == 1:
                        out_cols = [[out_col] for out_col in out_cols]
                    else:
                        raise TypeError("out_cols should be a nested list of str when both " +
                                        "cat_cols and target_cols have more than one elements")
                else:
                    for outs in out_cols:
                        assert isinstance(outs, list), "out_cols should be str, a list of str, " \
                                                       "or a nested list of str"
            else:
                raise TypeError("out_cols should be str, a list of str, or a nested list of str")
            assert len(out_cols) == len(cat_cols), "length of out_cols should be equal to " \
                                                   "length of cat_cols"
            for outs in out_cols:
                assert len(outs) == len(target_cols), "length of element in out_cols should be " \
                                                      "equal to length of target_cols"

        # calculate global mean for each target column
        target_mean_dict = {}
        if target_mean is not None:
            assert isinstance(target_mean, dict), "target_mean should be a dict"
            for target_col in target_cols:
                assert target_col in target_mean, \
                    "target column " + target_col + " should be in target_mean " + str(target_mean)
                target_mean_dict[target_col] = target_mean[target_col]
        else:
            global_mean_list = [F.mean(F.col(target_col)).alias(target_col)
                                for target_col in target_cols]
            target_mean_dict = self.df.select(*global_mean_list).collect()[0].asDict()
        for target_col in target_mean_dict:
            assert target_mean_dict[target_col] is not None, "mean of target column {} should " \
                                                             "not be None".format(target_col)

        # generate fold_col
        result_df = self.df
        if kfold > 1:
            if fold_col not in self.df.columns:
                if fold_seed is None:
                    result_df = result_df.withColumn(
                        fold_col,
                        F.monotonically_increasing_id() % F.lit(kfold)
                    )
                else:
                    result_df = result_df.withColumn(
                        fold_col, (F.rand(seed=fold_seed) * kfold).cast(IntegerType()))
            else:
                assert list(filter(lambda x: x[0] == fold_col and x[1] == "int",
                                   self.df.dtypes)), \
                    "fold_col should be integer type but get " + fold_col
        else:
            fold_col = None

        def gen_target_code(cat_out):
            cat_col = cat_out[0]
            out_col_list = cat_out[1]
            cat_col_name = gen_cols_name(cat_col, "_")

            sum_list = [F.sum(target_col).alias(cat_col_name + "_all_sum_" + target_col)
                        for target_col in target_cols]
            if isinstance(cat_col, str):
                org_all_df = result_df.groupBy(cat_col)
            else:
                org_all_df = result_df.groupBy(*cat_col)
            org_all_df = org_all_df.agg(*sum_list, F.count("*").alias(cat_col_name + "_all_count"))
            all_df = org_all_df
            for target_col, out_col in zip(target_cols, out_col_list):
                global_target_mean = target_mean_dict[target_col]
                all_func = udf(
                    lambda cat_sum, cat_count:
                    (cat_sum + global_target_mean * smooth) / (cat_count + smooth),
                    DoubleType())
                all_df = all_df.withColumn(out_col,
                                           all_func(cat_col_name + "_all_sum_" + target_col,
                                                    cat_col_name + "_all_count")) \
                    .drop(cat_col_name + "_all_sum_" + target_col)
            all_df = all_df.drop(cat_col_name + "_all_count")

            if kfold == 1:
                fold_df = all_df
            else:
                fold_sum_list = [F.sum(target_col).alias(cat_col_name + "_sum_" + target_col)
                                 for target_col in target_cols]
                if isinstance(cat_col, str):
                    fold_df = result_df.groupBy(cat_col, fold_col)
                else:
                    fold_df = result_df.groupBy(*cat_col, fold_col)
                fold_df = fold_df.agg(*fold_sum_list, F.count("*").alias(cat_col_name + "_count"))
                fold_df = fold_df.join(org_all_df, cat_col, how="left")
                for target_col, out_col in zip(target_cols, out_col_list):
                    global_target_mean = target_mean_dict[target_col]
                    target_func = udf(
                        lambda s_all, s, c_all, c:
                        None if c_all == c else
                        ((s_all - s) + global_target_mean * smooth) / ((c_all - c) + smooth),
                        DoubleType())
                    fold_df = fold_df.withColumn(
                        out_col,
                        target_func(cat_col_name + "_all_sum_" + target_col,
                                    cat_col_name + "_sum_" + target_col,
                                    cat_col_name + "_all_count",
                                    cat_col_name + "_count")
                    )
                    fold_df = fold_df.drop(cat_col_name + "_sum_" + target_col,
                                           cat_col_name + "_all_sum_" + target_col)
                fold_df = fold_df.drop(cat_col_name + "_count", cat_col_name + "_all_count")
                fold_df = fold_df.withColumnRenamed(fold_col, fold_col)

            out_target_mean_dict = {
                out_col: (target_col, target_mean_dict[target_col])
                for target_col, out_col in zip(target_cols, out_col_list)
            }
            return TargetCode(fold_df, cat_col, out_target_mean_dict), \
                TargetCode(all_df, cat_col, out_target_mean_dict)

        targets = list(map(gen_target_code, zip(cat_cols, out_cols)))
        fold_targets = [t[0] for t in targets]
        all_targets = [t[1] for t in targets]

        result_tbl = FeatureTable(result_df)
        result_tbl = encode_target_(result_tbl, fold_targets, drop_cat=drop_cat,
                                    drop_fold=drop_fold, fold_col=fold_col)

        return result_tbl, all_targets

    def encode_target(self, targets, target_cols=None, drop_cat=True):
        """
        Encode columns with the provided TargetCode(s).

        :param targets: TargetCode or a list of TargetCode.
        :param target_cols: str or a list of str. Selects part of target columns of which target
               encoding will be applied. Default is None and in this case all target columns
               contained in targets will be encoded.
        :param drop_cat: boolean. Whether to drop the categorical columns. Default is True.

        :return: A new FeatureTable which encodes each categorical column into group-specific
                 mean of target columns with provided TargetCodes.
        """
        if isinstance(targets, TargetCode):
            targets = [targets]
        elif isinstance(targets, list):
            for target_code in targets:
                assert isinstance(target_code, TargetCode), \
                    "element in targets should be TargetCode but get {}".format(type(target_code))
        else:
            raise TypeError("targets should be TargetCode or list of TargetCode")
        for target_code in targets:
            check_col_str_list_exists(self.df, target_code.cat_col, "TargetCode.cat_col in targets")
        if target_cols is not None:
            if isinstance(target_cols, str):
                target_cols = [target_cols]
            assert isinstance(target_cols, list), "target_cols should be str or list"

        result_tbl = FeatureTable(self.df)
        result_tbl = encode_target_(result_tbl, targets, target_cols=target_cols,
                                    drop_cat=drop_cat)

        return result_tbl

    def difference_lag(self, columns, sort_cols, shifts=1, partition_cols=None, out_cols=None):
        """
        Calculates the difference between two consecutive rows, or two rows with certain interval
        of the specified continuous columns. The table is first partitioned by partition_cols if it
        is not None, and then sorted by sort_cols before the calculation.

        :param columns: str or a list of str. Continuous columns to calculate the difference.
        :param sort_cols: str or a list of str. Columns by which the table is sorted.
        :param shifts: int or a list of int. Intervals between two rows.
        :param partition_cols: Columns by which the table is partitioned.
        :param out_cols: str, a list of str, or a nested list of str. When both columns and shifts
               has only one element, out_cols can be a single str. When columns or shifts has only
               one element, out_cols can be a list of str, and each element in out_cols corresponds
               to an element in shifts or columns. When it is a list of list of str, each inner list
               corresponds to a column in columns. Each element in the inner list corresponds to a
               shift in shifts. If it is None, the output column will be sort_cols + "_diff_lag_"
               + column + "_" + shift. Default is None.

        :return: A new FeatureTable with difference columns.
        """
        columns = str_to_list(columns, "columns")
        sort_cols = str_to_list(sort_cols, "sort_cols")
        nonnumeric_col_type = get_nonnumeric_col_type(self.df, columns)
        assert not nonnumeric_col_type, \
            "columns should be numeric but get " + \
            ", ".join(list(map(lambda x: x[0] + " of type " + x[1], nonnumeric_col_type)))
        if isinstance(shifts, int):
            shifts = [shifts]
        elif isinstance(shifts, list):
            for s in shifts:
                assert isinstance(s, int), "elements in shift should be integer but get " + str(s)
        else:
            raise TypeError("shift should be either int or a list of int")
        if partition_cols is not None:
            partition_cols = str_to_list(partition_cols, "partition_cols")
        if out_cols is None:
            sort_name = gen_cols_name(sort_cols)
            out_cols = [[sort_name + "_diff_lag_" + column + "_" + str(shift)
                         for shift in shifts] for column in columns]
        else:
            if isinstance(out_cols, str):
                assert len(columns) == 1 and len(shifts) == 1, \
                    "out_cols can be string only if both columns and shifts has only one element"
                out_cols = [[out_cols]]
            elif isinstance(out_cols, list):
                if all(isinstance(out_col, str) for out_col in out_cols):
                    if len(columns) == 1:
                        out_cols = [out_cols]
                    elif len(shifts) == 1:
                        out_cols = [[out_col] for out_col in out_cols]
                    else:
                        raise TypeError("out_cols should be a list of list of str when both " +
                                        "columns shifts have more than one elements")
                else:
                    for outs in out_cols:
                        assert isinstance(outs, list), "out_cols should be str, a list of str, " \
                                                       "or a list of lists of str"
            else:
                raise TypeError("out_cols should be str, a list of str, or a list of lists of str")
            assert len(out_cols) == len(columns), "length of out_cols should be equal to length " \
                                                  "of columns"
            for outs in out_cols:
                assert len(outs) == len(shifts), "length of element in out_cols should be " \
                                                 "equal to length of shifts"

        result_df = self.df
        if partition_cols is None:
            partition_window = Window.orderBy(*sort_cols)
        else:
            partition_window = Window.partitionBy(*partition_cols).orderBy(*sort_cols)
        for column, outs in zip(columns, out_cols):
            diff_func = udf(lambda a, b: a - b if a is not None and b is not None else None,
                            self.df.schema[column].dataType)
            for shift, out in zip(shifts, outs):
                result_df = result_df.withColumn(out, F.lag(column, shift).over(partition_window))
                result_df = result_df.withColumn(out, diff_func(column, out))

        return FeatureTable(result_df)


class StringIndex(Table):
    def __init__(self, df, col_name):
        super().__init__(df)
        cols = df.columns
        assert len(cols) >= 2, "StringIndex should have >= 2 columns: col_name, id and other " \
                               "columns"
        assert "id" in cols, "id should be a column of the DataFrame"
        assert col_name in cols, col_name + " should be a column of the DataFrame"
        self.col_name = col_name

    @classmethod
    def read_parquet(cls, paths, col_name=None):
        """
        Loads Parquet files as a StringIndex.

        :param paths: str or a list of str. The path/paths to Parquet file(s).
        :param col_name: str. The column name of the corresponding categorical column. If
               col_name is None, the file name will be used as col_name.

        :return: A StringIndex.
        """
        if not isinstance(paths, list):
            paths = [paths]
        if col_name is None and len(paths) >= 1:
            col_name = os.path.basename(paths[0]).split(".")[0]
        return cls(Table._read_parquet(paths), col_name)

    @classmethod
    def from_dict(cls, indices, col_name):
        """
        Create the StringIndex from a dict of indices.

        :param indices: dict, the key is the categorical column, and the value is the
               corresponding index. We assume that the key is a str and the value
               is a int.
        :param col_name: str. The column name of the categorical column.

        :return: A StringIndex.
        """
        spark = OrcaContext.get_spark_session()
        if not isinstance(indices, dict):
            raise ValueError('indices should be dict, but get ' + indices.__class__.__name__)
        if not col_name:
            raise ValueError('col_name should be str, but get None')
        if not isinstance(col_name, str):
            raise ValueError('col_name should be str, but get ' + col_name.__class__.__name__)
        indices = map(lambda x: {col_name: x[0], 'id': x[1]}, indices.items())
        schema = StructType([StructField(col_name, StringType(), False),
                             StructField("id", IntegerType(), False)])
        df = spark.createDataFrame((Row(**x) for x in indices), schema=schema)
        return cls(df, col_name)

    def to_dict(self):
        """
        Convert the StringIndex to a dict, with the categorical features as keys and indices
        as values.
        Note that you may only call this if the StringIndex is small.

        :return: A dict for the mapping from string to index.
        """
        cols = self.df.columns
        index_id = cols.index("id")
        col_id = cols.index(self.col_name)
        rows = self.df.collect()
        res_dict = {}
        for row in rows:
            res_dict[row[col_id]] = row[index_id]
        return res_dict

    def _clone(self, df):
        return StringIndex(df, self.col_name)

    def write_parquet(self, path, mode="overwrite"):
        """
        Write the StringIndex to Parquet file.

        :param path: str. The path to the Parquet file. Note that the col_name
               will be used as basename of the Parquet file.
        :param mode: str. One of "append", "overwrite", "error" or "ignore".
               append: Append the contents of this StringIndex to the existing data.
               overwrite: Overwrite the existing data.
               error: Throw an exception if the data already exists.
               ignore: Silently ignore this operation if the data already exists.
        """
        path = path + "/" + self.col_name + ".parquet"
        write_parquet(self.df, path, mode)


class TargetCode(Table):
    def __init__(self, df, cat_col, out_target_mean):
        """
        Target Encoding output used for encoding new FeatureTables, which consists of the encoded
        categorical column or column group and the target encoded columns (mean statistics of
        the categorical column or column group).

        :param df: Target encoded data.
        :param cat_col: str or list of str. The categorical column or column group encoded in the
               original FeatureTable.
        :param out_target_mean: dict, the key is the target encoded output column in this
               TargetCode, and the value is a tuple of the target column in the original
               FeatureTable together with the target column's global mean in the original
               FeatureTable.
               For example: {"col3_te_target1": ("target1", 3.0)}, and in this case cat_col for this
               TargetCode should be "col3".
        """
        super().__init__(df)
        self.cat_col = cat_col
        self.out_target_mean = out_target_mean

        check_col_str_list_exists(df, cat_col, "cat_col")

        assert isinstance(out_target_mean, dict), "out_target_mean should be dict"

    def _clone(self, df):
        return TargetCode(df, self.cat_col, self.out_target_mean)

    def rename(self, columns):
        assert isinstance(columns, dict), "columns should be a dictionary of {'old_name1': " \
                                          "'new_name1', 'old_name2': 'new_name2'}"
        new_df = self.df
        new_cat_col = self.cat_col
        new_out_target_mean = self.out_target_mean
        for old_name, new_name in columns.items():
            new_df = new_df.withColumnRenamed(old_name, new_name)
            if isinstance(self.cat_col, str) and old_name == self.cat_col:
                new_cat_col = new_name
            elif isinstance(self.cat_col, list):
                for i in range(len(self.cat_col)):
                    if self.cat_col[i] == old_name:
                        new_cat_col[i] = new_name
            elif old_name in self.out_target_mean:
                new_out_target_mean[new_name] = new_out_target_mean.pop(old_name)
        return TargetCode(new_df, new_cat_col, new_out_target_mean)
