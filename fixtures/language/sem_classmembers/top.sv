package cm_pkg;
  import "DPI-C" function int c_add(int a, int b);

  class base_item;
    static int created;
    rand bit [7:0] addr;
    rand bit [7:0] data;
    bit clk;
    constraint c_addr { addr inside {[8'h10:8'h1F]}; }
    covergroup cg_item @(posedge clk);
      cp_addr: coverpoint addr;
    endgroup
    function new();
      cg_item = new();
      created++;
    endfunction
    static function int count();
      return created;
    endfunction
  endclass

  class ext_item extends base_item;
    constraint c_data { data > 8'h80; }
    function int total();
      return c_add(addr, data);
    endfunction
  endclass
endpackage

module cm_top;
  import cm_pkg::*;
  ext_item it;
  int n;
  initial begin
    it = new();
    n = base_item::count() + it.total();
  end
endmodule
