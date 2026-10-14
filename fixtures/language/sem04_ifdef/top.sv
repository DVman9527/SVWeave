`ifdef USE_A
module branch_a(output logic y);
  assign y = 1'b1;
endmodule
`else
module branch_b(output logic y);
  assign y = 1'b0;
endmodule
`endif

module sem04_top(output logic y);
`ifdef USE_A
  branch_a u_branch(.y(y));
`else
  branch_b u_branch(.y(y));
`endif
endmodule

module sem04_never_instantiated(output logic z);
  assign z = 1'b0;
endmodule
