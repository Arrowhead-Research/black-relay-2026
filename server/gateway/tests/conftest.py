from argon2 import PasswordHasher

ADMIN_PASSWORD = "correct horse battery staple"
# Cheap parameters keep the tests fast; verification reads them back out of the hash string.
ADMIN_HASH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(ADMIN_PASSWORD)
