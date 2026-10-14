module dp2_leaf;
  parameter int P = 0;
endmodule

module dp2_top;
  dp2_leaf u ();
  defparam u.NO_SUCH = 1;  // target does not exist: must not become a fabricated reference
endmodule
