module cov_top;
  import cov_pkg::*;
  logic [WIDTH-1:0] packed_value;
  logic unpacked_value[WIDTH];
  logic [7:0] sink;
  assign sink = make_value();
  assign packed_value = sink;
endmodule
