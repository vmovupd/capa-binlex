# capa-binlex
capa-binlex is a script that:
- extracts capabilities from a file using CAPA; 
- searches for functions and/or basic blocks that triggered a match for CAPA rule in Milvus database through Binlex server API;
- outputs the results to Markdown file or displays them as JSON. 

The script can be used to speed up malware analysis and help in classification of malware samples by identifying code reusage.

# Dependencies
[CAPA](https://github.com/mandiant/capa) version 9.1.0 used through flare-capa module. 
[Binlex](https://github.com/c3rb3ru5d3d53c/binlex) in a form of:
- Python binding from commit [5db0572](https://github.com/c3rb3ru5d3d53c/binlex/commit/5db0572c6a4f14b72ff6a1357db582c69d91ecbe)
- Binlex [client](https://github.com/vmovupd/binlex/blob/master/scripts/libblclient/libblclient/client.py) and [server](https://github.com/vmovupd/binlex/tree/master/scripts/blserver) from my fork which are [not yet merged](https://github.com/c3rb3ru5d3d53c/binlex/pull/159) to Binlex repository

# Config
The script supports configuration via command line arguments as well as via file with YAML format. It is required to specify path to CAPA rules directory, while other parameters have default values.

Config is explained in [demo configuration file](https://github.com/vmovupd/capa-binlex/blob/main/capa-binlex.yml)

# Limitations
First of all, the script depends on *CAPA rules* which may produce false positives and false negatives. While the first one can be mitigated with enough data (many examples of the same malware functionality) in Milvus database, the latter can only be lowered by improving the CAPA rules' logic. Second, the script also depends on the *data stored in Milvus database*: the more analyzed functions/basic blocks stored in Milvus, the bigger is a probability of identifying a similar function/basic block inside the malware sample. Third, the obfuscation and virtualization would drastically decrease similarity scores between two functions as well as CAPA matches. In that case, it is best to detect the obfuscation/virtualization with CAPA rules or other tools and do not process the function or, in case of virtualization, the whole binary. Although, depending on the obfuscation technique used, it may still be possible to search the function in Milvus database with lower similarity thresholds, e.g. in case the obfuscation modifies a small part of the function or similarly obfuscated function is already stored in Milvus database.

There may be issues when using two different disassemblers, e.g. vivisect for CAPA and capstone for Binlex. It may result in Binlex not identifying the functions/basic blocks at particular addresses. By default, the warning is printed and the function/basic block will be marked as not found in Markdown results.