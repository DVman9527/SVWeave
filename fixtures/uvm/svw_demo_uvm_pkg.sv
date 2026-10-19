package svw_demo_uvm_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  class svw_driver extends uvm_driver #(uvm_sequence_item);
    `uvm_component_utils(svw_driver)
    function new(string name = "svw_driver", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual task run_phase(uvm_phase phase);
      `uvm_info("SVW_DRV", "driver fixture", UVM_LOW)
    endtask
  endclass

  class svw_monitor extends uvm_monitor;
    `uvm_component_utils(svw_monitor)
    function new(string name = "svw_monitor", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual task run_phase(uvm_phase phase);
      `uvm_info("SVW_MON", "monitor fixture", UVM_LOW)
    endtask
  endclass

  class svw_env extends uvm_env;
    `uvm_component_utils(svw_env)
    svw_driver drv;
    svw_monitor mon;
    function new(string name = "svw_env", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      drv = svw_driver::type_id::create("drv", this);
      mon = svw_monitor::type_id::create("mon", this);
    endfunction
  endclass

  class svw_smoke_test extends uvm_test;
    `uvm_component_utils(svw_smoke_test)
    svw_env env;
    function new(string name = "svw_smoke_test", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      env = svw_env::type_id::create("env", this);
    endfunction
  endclass
endpackage
