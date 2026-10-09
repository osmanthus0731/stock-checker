"""Only this collection is writable; mutations are atomic document operations."""
from copy import deepcopy
from uuid import uuid4
from pymongo.errors import DuplicateKeyError
from .domain import utc_now


class Conflict(Exception):
    pass


class Repository:
    def __init__(self, collection):
        self.collection = collection

    def create(self, data, username):
        for _ in range(5):
            number = "LOCAL-PO-" + uuid4().hex[:16].upper()
            doc = deepcopy(data)
            doc.update(_id=number, number=number, status="draft", revision=1,
                       created_by=username, updated_by=username, created_at=utc_now(), updated_at=utc_now())
            doc["history"] = [{"action": "created", "by": username, "at": doc["created_at"]}]
            try:
                self.collection.insert_one(doc)
                return doc
            except DuplicateKeyError:
                continue
        raise Conflict("Could not reserve a unique reference. Please retry.")

    def get(self, number, scope):
        return self.collection.find_one({"_id": number, **scope})

    def update(self, number, revision, data, username, scope):
        data = deepcopy(data)
        data.update(updated_by=username, updated_at=utc_now())
        result = self.collection.update_one(
            {"_id": number, "revision": revision, "status": "draft", **scope},
            {"$set": data, "$inc": {"revision": 1},
             "$push": {"history": {"action": "edited", "by": username, "at": data["updated_at"]}}})
        if not result.matched_count:
            raise Conflict("This PO changed or is locked. Reload before continuing.")
        return self.get(number, scope)

    def transition(self, number, revision, status, username, scope):
        allowed = ["draft"] if status == "finalised" else ["draft", "finalised"]
        stamp = utc_now()
        result = self.collection.update_one(
            {"_id": number, "revision": revision, "status": {"$in": allowed}, **scope},
            {"$set": {"status": status, "updated_at": stamp, "updated_by": username,
                      status + "_at": stamp, status + "_by": username},
             "$inc": {"revision": 1},
             "$push": {"history": {"action": status, "by": username, "at": stamp}}})
        if not result.matched_count:
            raise Conflict("This PO changed or the status transition is no longer available. Reload it.")
        return self.get(number, scope)

    def duplicate(self, original, username):
        keys = ("schema_version", "layout_version", "company", "supplier", "date", "delivery_date",
                "terms", "replacement", "currency", "items", "subtotal", "discount_type",
                "discount_value", "discount", "total")
        data = {k: deepcopy(original[k]) for k in keys}
        data.update(legacy_number="", duplicated_from=original["number"])
        return self.create(data, username)
