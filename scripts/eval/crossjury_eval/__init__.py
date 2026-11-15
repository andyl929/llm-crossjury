"""M1.7 eval harness: GPQA Diamond and SimpleQA through crossjury (see docs/evals.md).

Dataset items are downloaded at run time into the user cache directory and never
written into the repository. GPQA asks that its items stay offline, so nothing in this
package prints or persists GPQA question text outside the cache and the output
directory (both outside the repository).
"""
