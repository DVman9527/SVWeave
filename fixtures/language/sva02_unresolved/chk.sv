module chk(input logic clk, input logic a);
  property p_unknown;
    @(posedge clk) a |-> missing_sig;
  endproperty
  ap_unknown: assert property (p_unknown);
endmodule

bind no_such_module chk u_bad (.clk(clk), .a(a));
