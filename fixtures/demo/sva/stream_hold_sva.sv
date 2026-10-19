`default_nettype none
module stream_hold_sva #(
  parameter int unsigned DATA_W = 32
) (
  input logic              clk,
  input logic              rst_n,
  input logic              valid,
  input logic              ready,
  input logic [DATA_W-1:0] data
);
  property p_stall_holds;
    @(posedge clk) disable iff (!rst_n)
      (valid && !ready) |=> (valid && $stable(data));
  endproperty

  ap_stall_holds: assert property (p_stall_holds);
endmodule
`default_nettype wire
