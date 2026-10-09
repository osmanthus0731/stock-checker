"""Promote existing worker accounts without creating starter users. Dry run by default."""
import argparse
import os
import sys
from pathlib import Path
from uuid import uuid4

import certifi
from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inventory_platform.domain import stamp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    uri = os.getenv('MONGO_URI') or os.getenv('MONGO_URL')
    options = {'serverSelectionTimeoutMS': 10000}
    if uri.startswith('mongodb+srv') or 'mongodb.net' in uri:
        options.update(tls=True, tlsCAFile=certifi.where())
    with MongoClient(uri, **options) as client:
        db = client[os.getenv('MONGO_DB', 'inventory')]
        workers = list(db.users.find({'role': 'worker'}, {'username': 1, 'role': 1}))
        print({'promote_to_admin': [user['username'] for user in workers], 'apply': args.apply})
        if not args.apply or not workers:
            return
        with client.start_session() as session:
            with session.start_transaction():
                for user in workers:
                    result = db.users.update_one({'_id': user['_id'], 'role': 'worker'},
                                                 {'$set': {'role': 'admin', 'updated_at': stamp()},
                                                  '$inc': {'account_revision': 1}}, session=session)
                    if result.modified_count != 1:
                        raise RuntimeError('Account changed during promotion; transaction cancelled.')
                    db.audit_events.insert_one({'_id': str(uuid4()), 'action': 'user_promoted_to_admin',
                                                'timestamp': stamp(), 'user_id': 'system',
                                                'username': 'system', 'target_user_id': str(user['_id']),
                                                'target_username': user['username'],
                                                'before_role': 'worker', 'after_role': 'admin'}, session=session)
        print('Promoted:', len(workers))


if __name__ == '__main__':
    main()
