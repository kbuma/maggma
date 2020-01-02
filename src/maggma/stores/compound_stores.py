""" Special stores that combine underlying Stores together """
from typing import List, Iterator, Tuple, Optional, Union, Dict, Any
from datetime import datetime
from itertools import groupby
from pydash import set_
from pymongo import MongoClient
from monty.dev import deprecated
from maggma.core import Store, Sort
from maggma.stores import MongoStore


class JointStore(Store):
    """Store corresponding to multiple collections, uses lookup to join"""

    def __init__(
        self,
        database: str,
        collection_names: List[str],
        host: str = "localhost",
        port: int = 27017,
        username: str = "",
        password: str = "",
        master: Optional[str] = None,
        merge_at_root: bool = False,
        **kwargs
    ):
        """
        Args:
            database: The database name
            collection_names: list of all collections
                to join
            host: Hostname for the database
            port: TCP port to connect to
            username: Username for the collection
            password: Password to connect with
            master: name for the master collection
                if not specified this defaults to the first
                in collection_names list
        """
        self.database = database
        self.collection_names = collection_names
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._collection = None  # type: Any
        self.master = master or collection_names[0]
        self.merge_at_root = merge_at_root
        self.kwargs = kwargs
        super(JointStore, self).__init__(**kwargs)

    def name(self) -> str:
        """
        Return a string representing this data source
        """
        return self.master

    def connect(self, force_reset: bool = False):
        """
        Connects the underlying Mongo database and
        all collection connections
        Args:
            force_reset - whether to forcibly reset the connection
        """
        conn = MongoClient(self.host, self.port)
        db = conn[self.database]
        if self.username != "":
            db.authenticate(self.username, self.password)
        self._collection = db[self.master]
        self._has_merge_objects = (
            self._collection.database.client.server_info()["version"] > "3.6"
        )

    def close(self):
        """
        Closes underlying database connections
        """
        self._collection.database.client.close()

    @property  # type: ignore
    @deprecated("This will be removed in the future")
    def collection(self):
        """
        The root collection for this JointStore
        """
        return self._collection

    @property
    def nonmaster_names(self) -> List:
        """
        alll non-master collection names
        """
        return list(set(self.collection_names) - {self.master})

    @property
    def last_updated(self) -> datetime:
        """
        Special last_updated for this JointStore
        that checks all underlying collections
        """
        lus = []
        for cname in self.collection_names:
            store = MongoStore.from_collection(self._collection.database[cname])
            store.last_updated_field = self.last_updated_field
            lu = store.last_updated
            lus.append(lu)
        return max(lus)

    # TODO: implement update?
    def update(self, docs, update_lu=True, key=None, **kwargs):
        """
        Update documents into the underlying collections
        Not Implemented for JointStore
        """
        raise NotImplementedError("No update method for JointStore")

    def _get_store_by_name(self, name) -> MongoStore:
        """
        Gets an underlying collection as a mongoStore
        """
        if name not in self.collection_names:
            raise ValueError("Asking for collection not referenced in this Store")
        return MongoStore.from_collection(self._collection.database[name])

    def ensure_index(self, key, unique=False, **kwargs):
        """
        Can't ensure index for JointStore
        """
        raise NotImplementedError("No ensure_index method for JointStore")

    def _get_pipeline(self, criteria=None, properties=None, skip=0, limit=0):
        """
        Gets the aggregation pipeline for query and query_one
        Args:
            properties: properties to be returned
            criteria: criteria to filter by
            skip: docs to skip
            limit: limit results to N docs
        Returns:
            list of aggregation operators
        """
        pipeline = []
        for cname in self.collection_names:
            if cname is not self.master:
                pipeline.append(
                    {
                        "$lookup": {
                            "from": cname,
                            "localField": self.key,
                            "foreignField": self.key,
                            "as": cname,
                        }
                    }
                )

                if self.merge_at_root:
                    if not self._has_merge_objects:
                        raise Exception(
                            "MongoDB server version too low to use $mergeObjects."
                        )

                    pipeline.append(
                        {
                            "$replaceRoot": {
                                "newRoot": {
                                    "$mergeObjects": [
                                        {"$arrayElemAt": ["${}".format(cname), 0]},
                                        "$$ROOT",
                                    ]
                                }
                            }
                        }
                    )
                else:
                    pipeline.append(
                        {
                            "$unwind": {
                                "path": "${}".format(cname),
                                "preserveNullAndEmptyArrays": True,
                            }
                        }
                    )

        # Do projection for max last_updated
        lu_max_fields = ["${}".format(self.last_updated_field)]
        lu_max_fields.extend(
            [
                "${}.{}".format(cname, self.last_updated_field)
                for cname in self.collection_names
            ]
        )
        lu_proj = {self.last_updated_field: {"$max": lu_max_fields}}
        pipeline.append({"$addFields": lu_proj})

        if criteria:
            pipeline.append({"$match": criteria})
        if isinstance(properties, list):
            properties = {k: 1 for k in properties}
        if properties:
            pipeline.append({"$project": properties})

        if skip > 0:
            pipeline.append({"$skip": skip})

        if limit > 0:
            pipeline.append({"$limit": limit})
        return pipeline

    def query(
        self,
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Sort]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Dict]:
        pipeline = self._get_pipeline(
            criteria=criteria, properties=properties, skip=skip, limit=limit
        )
        agg = self._collection.aggregate(pipeline)
        for d in agg:
            yield d

    def groupby(
        self,
        keys: Union[List[str], str],
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Sort]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Tuple[Dict, List[Dict]]]:
        pipeline = self._get_pipeline(
            criteria=criteria, properties=properties, skip=skip, limit=limit
        )
        if not isinstance(keys, list):
            keys = [keys]
        group_id = {}  # type: Dict[str,Any]
        for key in keys:
            set_(group_id, key, "${}".format(key))
        pipeline.append({"$group": {"_id": group_id, "docs": {"$push": "$$ROOT"}}})

        agg = self._collection.aggregate(pipeline)

        for d in agg:
            yield d["_id"], d["docs"]

    def query_one(self, criteria=None, properties=None, **kwargs):
        """
        Get one document
        Args:
            properties([str] or {}): properties to return in query
            criteria ({}): filter for matching
            **kwargs: kwargs for collection.aggregate
        Returns:
            single document
        """
        # TODO: maybe adding explicit limit in agg pipeline is better as below?
        # pipeline = self._get_pipeline(properties, criteria)
        # pipeline.append({"$limit": 1})
        query = self.query(criteria=criteria, properties=properties, **kwargs)
        try:
            doc = next(query)
            return doc
        except StopIteration:
            return None

    def remove_docs(self, criteria: Dict):
        """
        Remove docs matching the query dictionary

        Args:
            criteria: query dictionary to match
        """
        raise NotImplementedError("No remove_docs method for JointStore")

    def __eq__(self, other: object) -> bool:
        """
        Check equality for JointStore
        other: other JointStore to compare with
        """
        if not isinstance(other, JointStore):
            return False

        fields = [
            "database",
            "collection_names",
            "host",
            "port",
            "master",
            "merge_at_root",
        ]
        return all(getattr(self, f) == getattr(other, f) for f in fields)


class ConcatStore(Store):
    """Store concatting multiple stores"""

    def __init__(self, *stores: Store, **kwargs):
        """
        Initialize a ConcatStore that concatenates multiple stores together
        to appear as one store
        """
        self.stores = stores
        super(ConcatStore, self).__init__(**kwargs)

    def name(self) -> str:
        """
        Return a string representing this data source
        """
        return self.stores[0].name

    def connect(self, force_reset: bool = False):
        """
        Connect all stores in this ConcatStore
        Args:
            force_reset (bool): Whether to forcibly reset the connection for
            all stores
        """
        for store in self.stores:
            store.connect(force_reset)

    def close(self):
        """
        Close all connections in this ConcatStore
        """
        for store in self.stores:
            store.close()

    @property  # type: ignore
    @deprecated
    def collection(self):
        raise NotImplementedError("No collection property for ConcatStore")

    @property
    def last_updated(self) -> datetime:
        """
        Finds the most recent last_updated across all the stores.
        This might not be the most usefull way to do this for this type of Store
        since it could very easily over-estimate the last_updated based on what stores
        are used
        """
        lus = []
        for store in self.stores:
            lu = store.last_updated
            lus.append(lu)
        return max(lus)

    def update(self, docs: Union[List[Dict], Dict], key: Union[List, str, None] = None):
        """
        Update documents into the Store
        Not implemented in ConcatStore

        Args:
            docs: the document or list of documents to update
            key: field name(s) to determine uniqueness for a
                 document, can be a list of multiple fields,
                 a single field, or None if the Store's key
                 field is to be used
        """
        raise NotImplementedError("No update method for ConcatStore")

    def distinct(
        self,
        field: Union[List[str], str],
        criteria: Optional[Dict] = None,
        all_exist: bool = False,
    ) -> Union[List[Dict], List]:
        """
        Get all distinct values for a field(s)
        For a single field, this returns a list of values
        For multiple fields, this return a list of of dictionaries for each unique combination

        Args:
            field: the field(s) to get distinct values for
            criteria : PyMongo filter for documents to search in
            all_exist : ensure all fields exist for the distinct set
        """
        distincts = []
        for store in self.stores:
            distincts.extend(
                store.distinct(field=field, criteria=criteria, all_exist=all_exist)
            )

        if isinstance(field, str):
            return list(set(distincts))
        else:
            return [dict(s) for s in set(frozenset(d.items()) for d in distincts)]

    def ensure_index(self, key: str, unique: bool = False) -> bool:
        """
        Ensure an index is properly set. Returns whether all stores support this index or not
        Args:
            key: single key to index
            unique: Whether or not this index contains only unique keys

        Returns:
            bool indicating if the index exists/was created on all stores
        """
        return all([store.ensure_index(key, unique) for store in self.stores])

    def query(
        self,
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Sort]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Dict]:
        """
        Queries across all Store for a set of documents

        Args:
            criteria : PyMongo filter for documents to search in
            properties: properties to return in grouped documents
            sort: Dictionary of sort order for fields
            skip: number documents to skip
            limit: limit on total number of documents returned
        """
        # TODO: skip, sort and limit are broken. implement properly
        for store in self.stores:
            for d in store.query(criteria=criteria, properties=properties):
                yield d

    def groupby(
        self,
        keys: Union[List[str], str],
        criteria: Optional[Dict] = None,
        properties: Union[Dict, List, None] = None,
        sort: Optional[Dict[str, Sort]] = None,
        skip: int = 0,
        limit: int = 0,
    ) -> Iterator[Tuple[Dict, List[Dict]]]:
        """
        Simple grouping function that will group documents
        by keys.

        Args:
            keys: fields to group documents
            criteria : PyMongo filter for documents to search in
            properties: properties to return in grouped documents
            sort: Dictionary of sort order for fields
            skip: number documents to skip
            limit: limit on total number of documents returned

        Returns:
            generator returning tuples of (dict, list of docs)
        """
        if isinstance(keys, str):
            keys = [keys]

        docs = []
        for store in self.stores:
            temp_docs = list(
                store.groupby(
                    keys=keys,
                    criteria=criteria,
                    properties=properties,
                    sort=sort,
                    skip=skip,
                    limit=limit,
                )
            )
            for key, group in temp_docs:
                docs.extend(group)

        def key_set(d: Dict) -> Tuple:
            "index function based on passed in keys"
            test_d = tuple(d.get(k, None) for k in keys)
            return test_d

        sorted_docs = sorted(docs, key=key_set)
        for vals, group_iter in groupby(sorted_docs, key=key_set):
            id_dict = {key: val for key, val in zip(keys, vals)}
            yield id_dict, list(group_iter)

    def remove_docs(self, criteria: Dict):
        """
        Remove docs matching the query dictionary

        Args:
            criteria: query dictionary to match
        """
        raise NotImplementedError("No remove_docs method for JointStore")

    def __eq__(self, other: object) -> bool:
        """
        Check equality for ConcatStore
        other: other ConcatStore to compare with
        """
        if not isinstance(other, ConcatStore):
            return False

        fields = ["stores"]
        return all(getattr(self, f) == getattr(other, f) for f in fields)