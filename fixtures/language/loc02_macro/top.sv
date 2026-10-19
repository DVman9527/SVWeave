`include "defs.svh"
module loc02_a;
  `include "dup.svh"
  `MAKE_REG(arg_reg)
  `DECL_FIXED
endmodule

module loc02_b;
  `include "dup.svh"
  `DECL_FIXED
`line 100 "virtual_file.sv" 0
  logic after_line_reg;
endmodule

module loc02_top;
  loc02_a u_a();
  loc02_b u_b();
endmodule
