# Jules Remote Variation Workflow

## Operator Note

Labelling an eligible issue `runner:jules` requests an independent Jules variation.

Key operational details:
- The default Codex worker can continue on the same issue.
- Jules creates its branch and pull request remotely.
- Symphony does not download or validate its patch.
