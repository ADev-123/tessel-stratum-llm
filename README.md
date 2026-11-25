# Tessel Stratum LLM

Run large language models that are too big for any single machine by splitting them layer by layer across many small, fault-tolerant nodes.

Each node, such as an EC2 instance, holds only a few GB of the model. The nodes are connected as a cluster, can span several AWS accounts, and keep serving requests when individual nodes fail.

The goal is to let organizations run models far larger than one server's memory allows, using commodity cloud instances instead of a single large GPU machine.
