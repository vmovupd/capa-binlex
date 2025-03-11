from pathlib import Path
import struct, yaml, json

class Binary:
    """An object used for processing a binary with CAPA and Binlex
        
    Attributes:
        filepath (str):     Absolute path to the file
        bformat (str):      Binary format
        barch (str):        Binary architecture
        
        Initialized after calling process_capa:
        capa_rules (str):   Absolute path to the directory with CAPA rules
        capa_flirt (str):      Absolute path to the directory with FLIRT signatures
        
        hashes (dict):          Hash values for the binary file from CAPA metadata. The key is hash function name and the value is the hash
        capa_matches (dict):    CAPA matches for each rule that was not filtered. The key is CAPA rule name and the value is list with the following elements: 
                                    - rule match addresses (list), 
                                    - static scope (str), 
                                    - MBC and MITRE ATT&CK behaviours/techniques (dict): keys are 'mbc' and 'attack'; values are lists of behaviours/techniques (str)
        
        Initialized after calling process_binlex:
        binlex_capa_results (dict):     Contains CAPA rule name as key, dictionaries as values
                                        The nested dictionaries contain virtual addresses of CAPA matches as keys and lists of search results as values, however first element contains metadata from Binlex about the function/block in the list
                                        The list contains dictionaries and starting from the second element holds information about the function/block which has been found in Milvus database

        Initialized after calling render_md:
        markdown (str):     Markdown results of the processed binary. Output consists of metadata (file path, binary format, architecture, hashes) section and results for functions and/or basic blocks represented by two sections
                            Each 'results' section contains two pie charts (Rules/Addresses with a Binlex match) as well as one or two tables: 
                                (1) Binlex results for the CAPA matches which were found in Milvus database with statistic for search results and comparison scores; 
                                (2) statistic for function/basic block from CAPA match with indcation whether or not it was found in Milvus database
                            If there is no CAPA matches for rules with function and/or basic blocks, then one or both sections won't be present in the output
    """
    from binlex.controlflow import Graph
    from binlex.formats import PE, ELF
    from binlex import Config, Architecture
    
    def __init__(self, filepath: str, use_ida: bool = False, threads: int = 4):
        """Initializes Binary object
        
        Arguments:
            filepath (str):     A path to the file to be processed
            use_ida (bool):     Whether IDA backend shall be used for CAPA and Binlex or not
            threads (int):      A number of threads to be used by Binlex for multi-threaded operations
        
        Attributes initialized:
            public:
                filepath, bformat, barch
            private:
                capa_backend (str):         A backend to be used when processing the file with CAPA depending on the format and use_ida flag
                binlex_config (Config):     An instance of Binlex Config object
                binlex_cfg (Graph):         A control flow graph instance from Binlex for the processed file; not initialized if use_ida is True
                ida (bool):                 Whether IDA backend shall be used for CAPA and Binlex or not (use_ida)
        """
        import capa.main
        
        self.filepath = Path(filepath).resolve()._str
        try:
            with open(self.filepath, 'rb') as file:
                f, a, blf = '', '', ''
                file.seek(0x00)
                buffer = file.read(4)
                if buffer[:2] == b'\x4d\x5a': # PE
                    file.seek(0x3c)
                    pe_offset = struct.unpack('<I', file.read(4))[0]
                    file.seek(pe_offset)
                    pe_signature = file.read(4)
                    if pe_signature == b'\x50\x45\x00\x00': 
                        file.seek(pe_offset + 4) 
                        machine_type = file.read(2)
                        if machine_type == b'L\x01':  # IMAGE_FILE_MACHINE_I386
                            f, a, blf = "PE", "i386", self.PE
                        elif machine_type == b'd\x86':  # IMAGE_FILE_MACHINE_AMD64
                            f, a, blf = "PE", "amd64", self.PE
                        
                        # Check for .NET CLR header
                        if (a == "i386"):
                            file.seek(pe_offset + 232)
                        elif (a == "amd64"):
                            file.seek(pe_offset + 248)
                        clr_header = file.read(4)
                        if clr_header != b'\x00\x00\x00\x00':
                            f, a = "DOTNET", "cil"
                elif buffer == b'\x7fELF': #ELF
                    file.seek(0x12)
                    elf_machine = file.read(1)
                    if (elf_machine == b'\x03'): # x86
                        f, a, blf = "ELF", "i386", self.ELF
                    elif (elf_machine == b'\x3E'): # x86_64
                        f, a, blf = "ELF", "amd64", self.ELF
                
                if (f == '' and a == ''): # Header may be corrupted as well
                    print(f"Unsupported file format or architecture. File path: {self.filepath}")
                    sys.exit(1)
                
                self.bformat, self.barch, bl_format = f, a, blf
        except FileNotFoundError as e:
            print(f"File {self.filepath} is not found")
            sys.exit(1)
        
        self.__ida = use_ida
        self.__binlex_config = self.Config() # Initialize default config
        self.__binlex_config.general.threads = threads
                
        if self.__ida:
            self.__capa_backend = capa.main.BACKEND_IDA
        else:
            bl_file = bl_format(self.filepath, self.__binlex_config)
            img = bl_file.image()
            mmap = img.mmap()
            
            if self.bformat == "DOTNET":
                from binlex.disassemblers.custom.cil import Disassembler
                entry_point = bl_file.dotnet_entrypoint_virtual_addresses()
                executable_address = [bl_file.dotnet_metadata_token_virtual_addresses(), bl_file.dotnet_executable_virtual_address_ranges()]
                self.__capa_backend = capa.main.BACKEND_DOTNET
            else:
                from binlex.disassemblers.capstone import Disassembler
                entry_point = bl_file.entrypoint_virtual_addresses()
                executable_address = [bl_file.executable_virtual_address_ranges()]
                self.__capa_backend = capa.main.BACKEND_VIV
            
            # Create Disassembler on mapped binary image and architecture
            disasm = Disassembler(self.Architecture.from_str(self.barch), mmap, *executable_address, self.__binlex_config)
            # Create control flow graph
            self.__binlex_cfg = self.Graph(self.Architecture.from_str(self.barch), self.__binlex_config)
            # Disassemble the image
            disasm.disassemble_controlflow(entry_point, self.__binlex_cfg)
    
    def process_capa(self, rules_path: str, signatures_path: str = "", discard_lib_rules: bool = True, unwanted_rules: list = ["contain obfuscated stackstrings", "encode data using XOR"]):
        """Identify capabilities in the binary using CAPA with given rules, FLIRT signatures (only for vivisect and PE) and write results to capa_matches
        
        Arguments:
            rules_path (str):               A path to the CAPA rules to be used for capability identification
            signatures_path (str):          A path to the FLIRT signatures to be used for library functions identification; only for vivisect disassembler and PE format
            discard_lib_rules (bool):       Whether to discard library rules in the results or not
            unwanted_rules (list):          A list of rules which shall be discarded in the results
        
        Attributes initialized:
            public:
                capa_rules, capa_flirt, hashes, capa_matches
        """
        import capa.rules
        import capa.loader
        import capa.render.json
        import capa.capabilities.common
        from capa.features.common import OS_AUTO
        
        # Load rules and signatures from disk
        self.capa_rules = Path(rules_path)
        rules = capa.rules.get_rules([self.capa_rules])
        self.capa_flirt = Path(signatures_path)
        signatures = capa.loader.get_signatures(self.capa_flirt) if (signatures_path != "" and self.__capa_backend == capa.main.BACKEND_VIV) else []
        
        # Extract features and find capabilities
        extractor = capa.loader.get_extractor(
            Path(self.filepath), self.bformat.lower(), OS_AUTO, self.__capa_backend, signatures, should_save_workspace=False, disable_progress=True
        )
        capabilities = capa.capabilities.common.find_capabilities(rules, extractor, disable_progress=True)
        meta = capa.loader.collect_metadata([], Path(self.filepath), self.bformat.lower(), OS_AUTO, [self.capa_rules], extractor, capabilities)    
        capa_output = json.loads(capa.render.json.render(meta, rules, capabilities.matches))

        # Convert Path variables to str and resolve the path
        self.capa_rules = self.capa_rules.resolve()._str
        self.capa_flirt = self.capa_flirt.resolve()._str
        
        self.hashes = {}
        for key, value in list(capa_output["meta"]["sample"].items())[:3]:
            self.hashes[key] = value

        self.capa_matches = {}
        for rule_name, rule_content in capa_output["rules"].items():
            
            # Filter for unwanted rules, like the one that detect obfuscation
            if rule_name in unwanted_rules:
                continue

            parsed_rule = yaml.safe_load(rule_content.get("source", ""))
            # Discard lib rules
            if discard_lib_rules == True and (parsed_rule.get("rule").get("meta").get("lib", "") == True):
                continue
            
            # Filter for file, instruction scopes
            static_scope = parsed_rule.get("rule").get("meta").get("scopes").get("static", "")
            if static_scope not in {"function", "basic block"}:
                continue
            
            match_addresses = []
            for match_group in rule_content["matches"]:
                for match in match_group:
                    if match.get("type") == "absolute":
                        match_addresses.append(match["value"])

            mbc = []
            attack = []

            for key in ['mbc', 'attack']:
                if rule_content['meta'].get(key):
                    for item in rule_content['meta'][key]:
                        if len(item['parts']) in (2, 3):
                            formatted_str = "::".join(item['parts']) + f' [{item['id']}]'
                            if key == 'mbc':
                                mbc.append(formatted_str)
                            else:
                                attack.append(formatted_str)

            if match_addresses:
                self.capa_matches[rule_name] = [match_addresses, static_scope, {'mbc': mbc, 'attack': attack}]

    def process_binlex(self, server_url: str, server_api: str, database: str = "malware",
        gnn_similarity_threshold: float = 0.75, size_ratio_threshold: float = 0.75, combined_ratio_threshold: float = 0.75, minhash_score_threshold: float = 0.75, limit: int = 3, ignore_unnamed: bool = False):
        """Identify capabilities in the binary using CAPA with given rules, FLIRT signatures (only for vivisect and PE) and write results to capa_matches
        
        Arguments:
            server_url (str):                       A URL to Binlex server
            server_api (str):                       An API key to access Binlex server
            database (str):                         A database name to be used for searching (Milvus database name)
            gnn_similarity_threshold (float):       A threshold for GNN score for a function/basic block to be considered
            size_ratio_threshold (float):           A threshold for size ratio for a function/basic block to be considered
            minhash_score_threshold (float):        A threshold for Minhash score for a function/basic block to be considered
            combined_ratio_threshold (float):       A threshold for combined ratio (an average of Minhash and GNN scores) for a function/basic block to be considered
            limit (int):                            How many function/basic blocks shall be returned after performing search in Milvus database
            ignore_unnamed (bool):                  Whether to ignore the functions/basic blocks without names or not ('name' column in Milvus is empty)
        
        Attributes initialized:
            public:
                binlex_capa_results
            private:
                binlex_cfg (Graph):         A control flow graph instance from Binlex for the processed file (only if ida - another private attribute - is True), otherwise it is already initialized in __init__
        """
        from binlex.controlflow import Function, Block, FunctionJsonDeserializer, BlockJsonDeserializer
        from blclient import BLClient
        
        # Verify that Binlex server is working and database is found
        client = BLClient(url=server_url, api_key=server_api)
        status, databases = client.databases()
    
        if status != 200:
            print(f"Connection to {server_url} with the api key {server_api} resulted in HTTP status {status}")
            sys.exit(1)
    
        if database not in databases:
            print(f"Database {database} not found. Available databases: {databases}")
            sys.exit(1)
        
        # The IDA database is already opened by CAPA and is not closed by default, meaning we don't have to open database
        if self.__ida:
            from binlex.disassemblers.ida import IDA
            from binlex.disassemblers.ida import Disassembler
            bl_file = IDA() 
            img = bl_file.image()
            mmap = img.mmap()
            self.__binlex_cfg = self.Graph(self.Architecture.from_str(self.barch), self.__binlex_config)
            disasm = Disassembler(self.Architecture.from_str(self.barch), mmap, {0: img.size()}, self.__binlex_config)
            disasm.disassemble_controlflow(self.__binlex_cfg)

        searched_addr = {}
        self.binlex_capa_results = {}
        for capa_rulename, capa_match in self.capa_matches.items():
            bl_rule_result = {}
            rule_scope = capa_match[1]
            for match_addr in capa_match[0]:
                
                # Check if the address within that scope has been searched already (meaning the block/function has been searched as CAPA can match on both block and function at the same address OR two rules match block/function at the same address)
                if (match_addr, rule_scope) in searched_addr:
                    bl_rule_result[str(hex(match_addr))] = searched_addr[(match_addr, rule_scope)]
                    continue
                
                
                if rule_scope == "function":
                    bl_obj = Function(match_addr, self.__binlex_cfg)
                    jsondeserializer = FunctionJsonDeserializer
                    collection = 'function'
                elif rule_scope == "basic block":
                    bl_obj = Block(match_addr, self.__binlex_cfg)
                    jsondeserializer = BlockJsonDeserializer
                    collection = 'block'

                try:
                    status, vector = client.inference(bl_obj.to_dict())
                except OSError: # Issue with different disassemblers used
                    print(f"{rule_scope.capitalize()} at {str(hex(match_addr))} was not identified by Binlex disassembler. Continuing")
                    continue

                if status != 200:
                    print(f"Connection to {server_url} with the api key: {server_api} resulted in HTTP status {status}.")
                    sys.exit(1)

                status, search_results = client.search(
                    database=database,
                    collection=collection,
                    partition=self.barch,
                    offset=0,
                    limit=limit,
                    threshold=gnn_similarity_threshold,
                    vector=vector
                )
                if status != 200:
                    print(f"Connection to {server_url} with the api key: {server_api} resulted in HTTP status {status}. Error: {search_results}")
                    sys.exit(1)

                # The first (index 0) element is a dictionary of Binlex info on the function/basic block. Next elements are search results
                bl_addr_results = [
                    {
                        # vector is not used for markdown output
                        "vector": str(vector),
                        "number_of_instructions": str(bl_obj.number_of_instructions()),
                        "entropy": "{:.2f}".format(bl_obj.entropy()),
                        "size": str(bl_obj.size())
                    }
                ]
                
                if rule_scope == "function":
                    bl_addr_results[0].update(
                        {
                            "cyclomatic_complexity": str(bl_obj.cyclomatic_complexity()),
                            "average_instructions_per_block": "{:.2f}".format(bl_obj.average_instructions_per_block())
                        }
                    )
                
                for search_result in search_results:
                    # Ignore functions without names
                    if ignore_unnamed and (len(search_result['name']) == 0):
                        continue

                    search_result_data = search_result['data']
                    rhs_obj = jsondeserializer(json.dumps(search_result_data), self.__binlex_config)

                    size_ratio = self.bl_calculate_size_ratio(bl_obj.size(), rhs_obj.size())
                    if size_ratio < size_ratio_threshold:
                        continue

                    comparison = jsondeserializer(bl_obj.json(), self.__binlex_config).compare(rhs_obj)

                    if comparison is None:
                        continue

                    minhash_score = comparison.score.minhash()
                    if minhash_score is None or minhash_score < minhash_score_threshold:
                        continue

                    combined_score = (search_result['score'] + minhash_score) / 2.0
                    if combined_score < combined_ratio_threshold:
                        continue

                    bl_addr_results.append({
                        # ID, timestamp and username are not used for markdown output
                        "id": search_result['id'],
                        "timestamp": search_result['timestamp'],
                        "username": search_result['username'],
                        "name": search_result['name'],
                        "sha256": search_result['file_attributes']['sha256'],
                        "address": str(search_result_data['address']),
                        **(
                            {
                                "cyclomatic_complexity": str(search_result_data['cyclomatic_complexity']), 
                                "average_instructions_per_block": "{:.2f}".format(search_result_data['average_instructions_per_block'])
                            }
                        if rule_scope == "function" else {}),
                        "number_of_instructions": str(search_result_data['number_of_instructions']),
                        "entropy": "{:.2f}".format(search_result_data['entropy']),
                        "size": str(search_result_data['size']),
                        "gnn_similarity": "{:.2f}".format(search_result['score']),
                        "minhash_score": "{:.2f}".format(minhash_score),
                        "combined_score": "{:.2f}".format(combined_score),
                        "size_ratio": "{:.2f}".format(size_ratio)
                    })
                searched_addr[(match_addr, rule_scope)] = bl_addr_results
                bl_rule_result[str(hex(match_addr))] = bl_addr_results
            self.binlex_capa_results[capa_rulename] = bl_rule_result
        
        if self.__ida:
            bl_file.close_database()
        
    def render_md(self, hue: int = 130, lightness: int = 50, gnn: int = 10, minhash: int = 6, size: int = 3):
        """Construct Markdown output with pie charts and tables based on capa_matches and binlex_capa_results
        
        Arguments:
            hue (int):          Number that represents hue of the color for gradient in Binlex result tables
            lightness (int):    Number that represents lightness of the color for gradient in Binlex result tables
            gnn (int):          Weight/Confidence for GNN score
            minhash (int):      Weight/Confidence for Minhash score
            size (int):         Weight/Confidence for size ratio
        
        Attributes initialized:
            public:
                binlex_capa_results, markdown
            private:
                binlex_cfg (Graph):         A control flow graph instance from Binlex for the processed file (only if ida - another private attribute - is True), otherwise it is already initialized in __init__
        """
        # Normalize the weights
        weigh_sum = gnn + minhash + size
        gnn, minhash, size = gnn / weigh_sum, minhash / weigh_sum, size / weigh_sum
        
        md_diagrams = {}
        
        md_diagrams["function_bl_results"] = """<table border="1">
    <tr>
        <th rowspan="3">Rule name</th>
        <th rowspan="3">Address</th>
        <th colspan="12">Binlex results</th>
    </tr>
    <tr>
        <th rowspan="2">Name of the function</th>
        <th rowspan="2">SHA256 of the sample</th>
        <th rowspan="2">Address in the sample</th>
        <th colspan="5">Obfuscation statistic</th>
        <th colspan="4">Comparison scores</th>
    </tr>
    <tr>
        <th>Cyclomatic complexity</th>
        <th>Number of instructions</th>
        <th>Entropy</th>
        <th>Average instructions per block</th>
        <th>Size</th>
        <th>GNN</th>
        <th>Minhash</th>
        <th>Combined</th>
        <th>Size</th>
    </tr>\n"""
    
        md_diagrams["function_bl_stat"] = """<table border="1">
    <tr>
        <th rowspan="2">Rule name</th>
        <th rowspan="2">Address</th>
        <th rowspan="2">Binlex search result</th>
        <th colspan="5">Binlex stats</th>
    </tr>
    <tr>
        <th>Cyclomatic complexity</th>
        <th>Number of instructions</th>
        <th>Entropy</th>
        <th>Average instructions per block</th>
        <th>Size</th>
    </tr>\n"""
    
        md_diagrams["basic block_bl_results"] = """<table border="1">
    <tr>
        <th rowspan="3">Rule name</th>
        <th rowspan="3">Address</th>
        <th colspan="10">Binlex results</th>
    </tr>
    <tr>
        <th rowspan="2">Name of the block</th>
        <th rowspan="2">SHA256 of the sample</th>
        <th rowspan="2">Address in the sample</th>
        <th colspan="3">Obfuscation statistic</th>
        <th colspan="4">Comparison scores</th>
    </tr>
    <tr>
        <th>Number of instructions</th>
        <th>Entropy</th>
        <th>Size</th>
        <th>GNN</th>
        <th>Minhash</th>
        <th>Combined</th>
        <th>Size</th>
    </tr>\n"""
    
        md_diagrams["basic block_bl_stat"] = """<table border="1">
    <tr>
        <th rowspan="2">Rule name</th>
        <th rowspan="2">Address</th>
        <th rowspan="2">Binlex search result</th>
        <th colspan="3">Binlex stats</th>
    </tr>
    <tr>
        <th>Number of instructions</th>
        <th>Entropy</th>
        <th>Size</th>
    </tr>\n"""
        
        # Used to build pie charts. 'matches' indicate amount of CAPA matches (addresses inside rules, not unique) for function/basic block rules, while 'rules' indicate amount of CAPA rules overall that has static scope of function/basic block
        bl_match_counts = {
            "function": {"matches": 0, "no_bl_matches": 0, "rules": 0, "no_bl_rules": 0},
            "basic block": {"matches": 0, "no_bl_matches": 0, "rules": 0, "no_bl_rules": 0}
        }
        
        for rule_name, capa_rule_matches in self.binlex_capa_results.items():
            rule_scope = self.capa_matches[rule_name][1]
            rule_span, rule_html = 0, ""
            rule_matches = len(capa_rule_matches)
            first_iteration_rule = True

            bl_match_counts[rule_scope]["matches"] += rule_matches
            bl_match_counts[rule_scope]["rules"] += 1

            for capa_addr, match_info in capa_rule_matches.items():
                md_diagrams[f"{rule_scope}_bl_stat"] += f"""    <tr>
        <td{f">{rule_name}</td>\n        <td>{capa_addr}" if rule_matches == 1 else f" rowspan={rule_matches}>{rule_name}</td>\n        <td>{capa_addr}"  if first_iteration_rule else f">{capa_addr}"}</td>
        <td style="background-color: {"#EC441C;\">Not Found" if len(match_info) == 1 else "lightgreen;\">Found"}</td>\n""" + self.create_bl_row_meta(match_info[0], is_function=(True if rule_scope == "function" else False))
                first_iteration_rule = False

                if len(match_info) == 1:
                    bl_match_counts[rule_scope]["no_bl_matches"] += 1
                    continue

                match_html, match_span = "", 0
                first_iteration_match = True
                address_matches = len(match_info) - 1

                for binlex_result in match_info[1:]:
                    saturation = round((10 + 90 * (gnn * float(binlex_result["gnn_similarity"]) + minhash * float(binlex_result["minhash_score"]) + size * float(binlex_result["size_ratio"]))), 1)
                    match_html += f"""    <tr>{f"\n        <td>{capa_addr}</td>\n" if address_matches == 1 else f"\n        <td rowspan={address_matches}>{capa_addr}</td>\n" if first_iteration_match else "\n"}""" + self.create_bl_row(binlex_result, str(hue), str(saturation), str(lightness), is_function=(True if rule_scope == "function" else False))
                    first_iteration_match = False
                    match_span += 1
                
                rule_html += match_html
                rule_span += match_span

            if rule_span == 0:
                bl_match_counts[rule_scope]["no_bl_rules"] += 1
            else:
                md_diagrams[f"{rule_scope}_bl_results"] += f"""    <tr>\n        <td{f">{rule_name}" if rule_span == 1 else f" rowspan={rule_span}>{rule_name}"}</td>""" + rule_html[8:]
        
        
        for html in md_diagrams:
            md_diagrams[html] += "</table>"

        # Meta information goes first
        md_output = f"# Meta information\nFile path: {self.filepath}\nFormat: {self.bformat}\nArchitecture: {self.barch}\n" + ''.join(f"{key.upper()}: {value}\n" for key, value in self.hashes.items()) + f"CAPA rules used: {self.capa_rules}\nFLIRT signatures used: {self.capa_flirt}\n"
        
        for scope in ["function", "basic block"]:
            if bl_match_counts[scope]['rules']:
                pies = f"""```mermaid\npie showData title Rules with a Binlex match\n\t"Found" : {bl_match_counts[scope]["rules"] - bl_match_counts[scope]["no_bl_rules"]}\n\t"Not Found" : {bl_match_counts[scope]["no_bl_rules"]}\n```
```mermaid\npie showData title Addresses with a Binlex match\n\t"Found" : {bl_match_counts[scope]["matches"] - bl_match_counts[scope]["no_bl_matches"]}\n\t"Not Found" : {bl_match_counts[scope]["no_bl_matches"]}\n```"""
                md_output += f"# Results for {scope}s\n{pies}\n{f"{md_diagrams[f"{scope}_bl_results"]}\n" if "td" in md_diagrams[f"{scope}_bl_results"] else ""}{md_diagrams[f"{scope}_bl_stat"]}\n\n"
        
        self.markdown = md_output

    @staticmethod
    def create_bl_row(blsearch_match: dict, hue: str, saturation: str, lightness: str, is_function: bool) -> str:
        """Creates a row for table with Binlex search results without 'Rule name' and 'Address' columns
        
        Arguments:
            blsearch_match (dict):      A dictionary which holds information about one Binlex search result (function/basic block)
            hue (str):                  Number that represents hue of the color for gradient in the row
            saturation (str):           Number that represents saturation of the color for gradient in the row
            lightness (str):            Number that represents lightness of the color for gradient in the row
            is_function (bool):         Whether the search result is a function or not (affects the fields added to the row)
        
        Returns:
            str:    Created row for table with Binlex search results
        """
        return f"""        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["name"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["sha256"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["address"]}</td>{f"""\n        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["cyclomatic_complexity"]}</td>""" if is_function else ""}
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["number_of_instructions"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["entropy"]}</td>{f"""\n        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["average_instructions_per_block"]}</td>""" if is_function else ""}
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["size"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["gnn_similarity"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["minhash_score"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["combined_score"]}</td>
        <td style="background: hsl({hue}, {saturation}%, {lightness}%);">{blsearch_match["size_ratio"]}</td>
    </tr>\n"""
    
    @staticmethod
    def create_bl_row_meta(blstat: dict, is_function: bool) -> str:
        """Creates a row for table with Binlex statistic
        
        Arguments:
            blstat (dict):          A dictionary which holds statistic from Binlex on the function/basic block
            is_function (bool):     Whether the dictionary holding statistic is a function or not (affects the fields added to the row)
        
        Returns:
            str:    Created row for table with Binlex statistic
        """
        return f"""{f"""        <td>{blstat["cyclomatic_complexity"]}</td>\n""" if is_function else ""}        <td>{blstat["number_of_instructions"]}</td>
        <td>{blstat["entropy"]}</td>
{f"""        <td>{blstat["average_instructions_per_block"]}</td>\n""" if is_function else ""}        <td>{blstat["size"]}</td>
    </tr>\n"""
    
    @staticmethod
    def bl_calculate_size_ratio(len1: int, len2: int) -> float:
        """Calculate a size ratio for comparison in process_binlex"""
        if max(len1, len2) == 0:
            return 1.0
        return 1 - (abs(len1 - len2) / max(len1, len2))

def generate_output(binary: Binary, output_format: str, filename: str, **gradientargs):
    match output_format:
        case "markdown":
            binary.render_md(**gradientargs.get("hsl", {}), **gradientargs.get("weights", {}))
            with open(filename, "w") as f:
                f.write(binary.markdown)
            return filename
        case "json":
            return {
                "capa_matches": binary.capa_matches,
                "binlex_capa_matches": binary.binlex_capa_results
            }
        case _:
            print(f"Unknown output format {output_format} received.")
            sys.exit(1)

def main(args):    
    if args.config:
        try:
            with open(args.config, "r") as conf:
                config = yaml.safe_load(conf)
        except FileNotFoundError as e:
            print(f"Configuration file {args.config} is not found")
            sys.exit(1)
        
        binobj = Binary(args.file, **config['general'])
        binobj.process_capa(**config['capa'])
        binobj.process_binlex(
            **{**{k: v for k, v in config['binlex'].items() if k != 'comparison'}, **config['binlex']['comparison']}
        )
        print(generate_output(binary=binobj, **config["output"]))
    else:
        binobj = Binary(args.file, args.ida)
        binobj.process_capa(args.rules, args.signatures)
        binobj.process_binlex(args.url, args.api)
        
        print(generate_output(
            binobj, 
            output_format=args.format,
            filename=args.output
        ))

if __name__ == "__main__":
    import sys
    import argparse

    __developer__ = 'vmovupd'

    parser = argparse.ArgumentParser(
        description="Extract capabilities from a file using CAPA, search for function/basic blocks that triggered a match in Milvus database with Binlex server API and write the results to Markdown file or display as JSON",
        epilog=f'Developed by: {__developer__}'
    )
    parser.add_argument("--file", "-f", required=True, help="File to be processed")
    parser.add_argument("--config", "-c", help="Path to the configuration file")
    if (('--config' not in sys.argv) and ('-c' not in sys.argv)):
        parser.add_argument("--rules", "-r", required=True, help="Path to the directory with CAPA rules")
        parser.add_argument("--signatures", "-s", help="Path to the directory with FLIRT signatures to be used in CAPA (only for Vivisect and PE files)", default="")
        parser.add_argument("--ida", action='store_true', help="Use IDA as backend for Binlex and CAPA")
        parser.add_argument("--format", "-of", help="Output format", default="markdown", choices=['markdown', 'json'])
        parser.add_argument("--output", "-o", help="Output filename for markdown", default="output.md")
        parser.add_argument("--url", help="URL to Binlex server", default="http://127.0.0.1:5000/")
        parser.add_argument("--api", help="API key to access Binlex server", default="39248239c8ed937d6333a41874f1c8e310c5070703af30c06e67b0d308cb82c5")
    main(parser.parse_args())