package svw_uvm_cfgdb_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  class svw_cfg_env extends uvm_env;
    `uvm_component_utils(svw_cfg_env)
    int unsigned width;
    string tag;
    function new(string name = "svw_cfg_env", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      if (!uvm_config_db#(int unsigned)::get(this, "", "width", width))
        `uvm_info("SVW_CFG", "width not set", UVM_LOW)
      if (!uvm_config_db#(string)::get(this, "", "tag", tag))
        `uvm_info("SVW_CFG", "tag not set", UVM_LOW)
    endfunction
  endclass

  class svw_cfg_test extends uvm_test;
    `uvm_component_utils(svw_cfg_test)
    svw_cfg_env env;
    function new(string name = "svw_cfg_test", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      uvm_config_db#(int unsigned)::set(this, "env*", "width", 32);
      uvm_config_db#(int unsigned)::set(this, "env", "width", 16);
      uvm_config_db#(int)::set(this, "env", "width", 7);
      uvm_config_db#(string)::set(this, "*", "tag", "alpha");
      env = svw_cfg_env::type_id::create("env", this);
    endfunction
  endclass
endpackage
