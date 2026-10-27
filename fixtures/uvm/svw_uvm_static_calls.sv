// UVM-02/03: real UVM APIs, written argument order is deliberately non-formal order.
package svw_uvm_static_calls_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  class item extends uvm_sequence_item;
    `uvm_object_utils(item)
    function new(string name = "item");
      super.new(name);
    endfunction
  endclass

  class replacement extends item;
    `uvm_object_utils(replacement)
    function new(string name = "replacement");
      super.new(name);
    endfunction
  endclass

  // Same method spelling is NOT evidence that a call is a UVM API.
  class local_db;
    static function void set(int value); endfunction
    static function int get(); return 1; endfunction
  endclass

  class static_calls_test extends uvm_test;
    `uvm_component_utils(static_calls_test)
    int width;
    item request;
    function new(string name = "static_calls_test", uvm_component parent = null);
      super.new(name, parent);
    endfunction
    virtual function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      uvm_config_db#(int)::set(.value(32), .field_name("width"),
                              .cntxt(this), .inst_name("env.*"));
      if (!uvm_config_db#(int)::get(.value(width), .inst_name("env"),
                                   .field_name("width"), .cntxt(this)))
        width = 0;
      set_type_override_by_type(.override_type(replacement::get_type()),
                                .original_type(item::get_type()));
      set_inst_override_by_type(.override_type(replacement::get_type()),
                                .relative_inst_path("env.*"),
                                .original_type(item::get_type()));
      set_type_override("item", "replacement");
      set_inst_override("env.*", "item", "replacement");
      request = item::type_id::create(.parent(this), .name("request"));
      // Positional and explicit empty argument exercise omitted registry defaults.
      request = item::type_id::create("second", , "env");
      local_db::set(local_db::get());
    endfunction
  endclass
endpackage

module svw_uvm_static_calls_top;
  import uvm_pkg::*;
  import svw_uvm_static_calls_pkg::*;
  initial run_test("static_calls_test");
endmodule
