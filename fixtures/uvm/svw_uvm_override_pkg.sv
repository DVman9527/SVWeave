package svw_uvm_override_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  class svw_base_item extends uvm_sequence_item;
    `uvm_object_utils(svw_base_item)
    function new(string name = "svw_base_item");
      super.new(name);
    endfunction
  endclass

  class svw_item_a extends svw_base_item;
    `uvm_object_utils(svw_item_a)
    function new(string name = "svw_item_a");
      super.new(name);
    endfunction
  endclass

  class svw_item_b extends svw_base_item;
    `uvm_object_utils(svw_item_b)
    function new(string name = "svw_item_b");
      super.new(name);
    endfunction
  endclass

  class svw_ov_env extends uvm_env;
    `uvm_component_utils(svw_ov_env)
    function new(string name = "svw_ov_env", uvm_component parent = null);
      super.new(name, parent);
    endfunction
  endclass

  class svw_test_ov_a extends uvm_test;
    `uvm_component_utils(svw_test_ov_a)
    svw_ov_env env;
    function new(string name = "svw_test_ov_a", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      set_type_override_by_type(svw_base_item::get_type(), svw_item_a::get_type());
      env = svw_ov_env::type_id::create("env", this);
    endfunction
  endclass

  class svw_test_ov_b extends uvm_test;
    `uvm_component_utils(svw_test_ov_b)
    svw_ov_env env;
    function new(string name = "svw_test_ov_b", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      set_inst_override_by_type("env.*", svw_base_item::get_type(), svw_item_b::get_type());
      env = svw_ov_env::type_id::create("env", this);
    endfunction
  endclass
endpackage
