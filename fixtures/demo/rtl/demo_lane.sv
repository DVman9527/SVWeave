`default_nettype none
module demo_lane #(
  parameter int unsigned DATA_W = 32
) (
  input  logic              clk,
  input  logic              rst_n,
  input  logic              valid_i,
  output logic              ready_o,
  input  logic [DATA_W-1:0] data_i,
  output logic              valid_o,
  input  logic              ready_i,
  output logic [DATA_W-1:0] data_o
);
  assign valid_o = valid_i;
  assign ready_o = ready_i;
  assign data_o  = data_i;
endmodule
`default_nettype wire
